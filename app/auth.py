"""
app/auth.py
===========
★ 认证与鉴权（企业级的最大缺口）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么这是第一优先级？
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    这个项目做了五层 SQL 护栏、路由单向硬约束、阻断纪律、双层评审……
    但在此之前，**任何人 curl 一下 /api/chat 就能拿到全部经营数据**。

    安全边界比功能完善度更重要：**没有认证，前面所有护栏都失去意义** ——
    因为它们保护的是一扇没有门的房间。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
三层设计
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ① 认证（Authentication）：你是谁？—— JWT Bearer Token
    ② 授权（Authorization）：你能用哪些接口？—— 角色 → 接口权限
    ③ 数据权限（Data Scoping）：你能看哪些数据？—— 角色 → 类目/渠道白名单
                                                            ★ 下沉到指标层

    ★ 第 ③ 层是最容易被忽略、也最关键的一层。
      很多系统做了前两层就以为安全了，但如果一个"只看美妆类目"的运营
      能通过 /api/products 看到全部类目数据，那权限体系就是纸糊的。
      所以数据权限必须**下沉到数据访问层**，而不是在接口层过滤。
"""
import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ---------------------------------------------------------------------------
# 角色与权限矩阵
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """角色。"""

    ADMIN = "admin"        # 管理员：全部权限
    ANALYST = "analyst"    # 分析师：可查数据、可跑 Agent、可写
    VIEWER = "viewer"      # 只读：只能查确定性接口，不能跑 LLM（省钱 + 防滥用）


# 接口权限矩阵：路径前缀 → 允许的角色
#   ★ 默认拒绝：不在表里的路径视为拒绝，避免"忘了配就放行"这种经典漏洞。
PERMISSIONS: dict[str, set[Role]] = {
    # 系统类：所有登录用户可看
    "/api/health": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    "/api/schema": {Role.ADMIN, Role.ANALYST},
    # 确定性数据接口：只读角色也能用（不消耗 LLM 成本）
    "/api/metrics": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    "/api/quality": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    "/api/products": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    "/api/channels": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    # SQL 接口：能绕过指标层口径，风险较高，不给只读角色
    "/api/sql": {Role.ADMIN, Role.ANALYST},
    # 可观测类：工具清单与统计含内部实现信息，管理员与分析师可看
    #   ★ 注意：FastAPI 的路径匹配是精确的，所以 /api/tools 与
    #     /api/tools/stats 必须分别配置 —— 只配前者不会覆盖后者。
    #     实测踩过：只配了 /api/tools，导致分析师访问 /api/tools/stats
    #     被 403 拒绝。这类"父子路径"漏配是权限矩阵最常见的坑。
    "/api/tools": {Role.ADMIN, Role.ANALYST},
    "/api/tools/stats": {Role.ADMIN, Role.ANALYST},
    # 审计与安全状态：含用户清单，仅管理员
    "/api/audit/stats": {Role.ADMIN},
    # 工作流类：会消耗 LLM 成本
    "/api/chat": {Role.ADMIN, Role.ANALYST},
    "/api/requirement": {Role.ADMIN, Role.ANALYST},
    "/api/weekly-review": {Role.ADMIN, Role.ANALYST},
    # 认证自身
    "/api/auth/login": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
    "/api/auth/me": {Role.ADMIN, Role.ANALYST, Role.VIEWER},
}


class DataScope:
    """
    ★ 数据权限范围（第 ③ 层）。

    决定这个用户能看到哪些类目/渠道的数据。
    None 表示不限制（管理员）；否则是白名单。
    """

    def __init__(self, categories: list[str] | None = None,
                 channels: list[str] | None = None):
        self.categories = categories      # None = 全部
        self.channels = channels

    @property
    def unrestricted(self) -> bool:
        return self.categories is None and self.channels is None

    def filter_categories(self, values: list[str]) -> list[str]:
        if self.categories is None:
            return values
        return [v for v in values if v in self.categories]

    def allows_category(self, value: str) -> bool:
        return self.categories is None or value in self.categories

    def clause(self, column: str) -> tuple[str, list]:
        """
        生成 SQL 片段，供数据层直接拼进 WHERE。

        ★ 注意参数化：不要用字符串拼接值，避免 SQL 注入 ——
          即使是白名单，也要走参数绑定。
        """
        if self.categories is None or not self.categories:
            return "", []
        placeholders = ",".join("?" for _ in self.categories)
        return f" AND {column} IN ({placeholders})", list(self.categories)

    def describe(self) -> str:
        if self.unrestricted:
            return "全部数据"
        parts = []
        if self.categories is not None:
            parts.append(f"类目={self.categories}")
        if self.channels is not None:
            parts.append(f"渠道={self.channels}")
        return "；".join(parts)


@dataclass
class User:
    """用户。"""

    username: str
    role: Role
    display_name: str = ""
    scope: DataScope = field(default_factory=DataScope)
    # 每日 LLM 调用配额（None = 用全局默认）
    daily_llm_quota: int | None = None
    disabled: bool = False

    def can_access(self, path: str) -> bool:
        """该用户能否访问某路径（默认拒绝）。"""
        allowed = PERMISSIONS.get(path)
        if allowed is None:
            # 未配置的路径：只有管理员可访问（默认拒绝的宽松版）
            return self.role == Role.ADMIN
        return self.role in allowed

    def to_public(self) -> dict:
        return {
            "username": self.username,
            "role": self.role.value,
            "display_name": self.display_name,
            "scope": self.scope.describe(),
            "daily_llm_quota": self.daily_llm_quota,
        }


# ---------------------------------------------------------------------------
# 密码哈希（PBKDF2-HMAC-SHA256）
# ---------------------------------------------------------------------------

_PBKDF2_ROUNDS = 200_000      # 按 OWASP 2023 建议量级


def hash_password(password: str, salt: str | None = None) -> str:
    """
    生成密码哈希，格式：pbkdf2_sha256$rounds$salt$hash

    ★ 为什么用 PBKDF2 而不是 md5/sha256 直哈希？
      直哈希速度太快，暴力破解成本低。PBKDF2 通过 20 万次迭代
      人为拖慢计算，使暴力破解代价大幅提高。
      这是"慢哈希"的设计意图 —— 慢是特性，不是缺陷。

    ★ Python 标准库自带 hashlib.pbkdf2_hmac，不需要额外依赖。
    """
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                             _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """
    校验密码。

    ★ 必须用 hmac.compare_digest 做**常量时间比较**，
      不能用 == —— 后者会在第一个不同字符处提前返回，
      攻击者可通过响应时间差异逐字符推断哈希值（时序攻击）。
    """
    try:
        algo, rounds_s, salt, expected = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(),
                                 int(rounds_s))
        return hmac.compare_digest(dk.hex(), expected)
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# JWT（HS256，标准库实现，避免额外依赖）
# ---------------------------------------------------------------------------

JWT_ALG = "HS256"


def _b64e(data: bytes) -> str:
    return urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return urlsafe_b64decode(text + padding)


def _sign(message: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return _b64e(sig)


def create_token(username: str, role: str, secret: str,
                 ttl_seconds: int = 8 * 3600,
                 extra: dict | None = None) -> str:
    """
    签发 JWT。

    ★ 生产建议：用 PyJWT 库并支持 RS256（非对称签名），
      这样验证方不需要持有签名密钥，能更好地隔离权限。
      这里用标准库实现 HS256 是为了让项目零额外依赖、可直接运行 ——
      但**必须把 SECRET 放在环境变量/KMS 里**，不能硬编码。
    """
    header = {"alg": JWT_ALG, "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": secrets.token_hex(8),      # 唯一 ID，便于未来做吊销
    }
    if extra:
        payload.update(extra)

    segs = [
        _b64e(json.dumps(header, separators=(",", ":")).encode()),
        _b64e(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segs).encode()
    segs.append(_sign(signing_input, secret))
    return ".".join(segs)


class TokenError(Exception):
    """Token 无效。"""


def decode_token(token: str, secret: str) -> dict:
    """
    校验并解析 JWT。

    校验三件事，缺一不可：
      ① 签名有效（防伪造）
      ② 未过期（exp）
      ③ 结构完整
    """
    try:
        header_b64, payload_b64, sig = token.split(".")
    except ValueError as exc:
        raise TokenError("Token 格式不正确") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected_sig = _sign(signing_input, secret)
    # ★ 常量时间比较，防时序攻击
    if not hmac.compare_digest(sig, expected_sig):
        raise TokenError("Token 签名无效")

    try:
        payload = json.loads(_b64d(payload_b64))
    except Exception as exc:                            # noqa: BLE001
        raise TokenError("Token 载荷无法解析") from exc

    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        raise TokenError("Token 已过期，请重新登录")

    return payload


# ---------------------------------------------------------------------------
# 用户存储
# ---------------------------------------------------------------------------

class UserStore:
    """
    用户存储。

    ★ 当前实现从环境变量读取（`EC_USERS` 为 JSON），适合单机部署。
      生产环境应替换为：LDAP / OIDC / 企业 SSO / 数据库用户表。
      接口保持不变即可 —— 这是"可替换基础设施"的做法。

    环境变量格式示例（EC_USERS）：
        {"alice": {"password_hash": "pbkdf2...", "role": "admin",
                   "display_name": "Alice", "categories": null},
         "bob":   {"password_hash": "pbkdf2...", "role": "analyst",
                   "display_name": "Bob", "categories": ["美妆", "数码"],
                   "daily_llm_quota": 50}}
    """

    def __init__(self, raw: str | None = None):
        self._users: dict[str, User] = {}
        self._hashes: dict[str, str] = {}
        self._load(raw if raw is not None else os.getenv("EC_USERS", ""))

    def _load(self, raw: str) -> None:
        if not raw.strip():
            self._load_development_defaults()
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._load_development_defaults()
            return

        for username, spec in data.items():
            self._hashes[username] = spec.get("password_hash", "")
            self._users[username] = User(
                username=username,
                role=Role(spec.get("role", "viewer")),
                display_name=spec.get("display_name", username),
                scope=DataScope(spec.get("categories"), spec.get("channels")),
                daily_llm_quota=spec.get("daily_llm_quota"),
                disabled=bool(spec.get("disabled", False)),
            )

    def _load_development_defaults(self) -> None:
        """
        开发环境默认账号。

        ⚠️ 这是**开发便利**，不是生产配置。生产必须通过 EC_USERS 注入。
           默认账号会在启动日志与健康检查里被标记出来，
           避免"以为改了其实没改"。

        ★ 演示数据权限时注意：analyst 只给**两个**类目，
          这样才能真正看出"数据被过滤了"。
          如果给全部 7 个类目，等于没限制，也就演示不出第 ③ 层的作用 ——
          这一点是实测时才发现的：最初给全类目，结果与 admin 输出完全一致，
          看起来"权限没生效"，实际是测试数据不严谨。
        """
        defaults = [
            ("admin", "admin123", Role.ADMIN, "管理员", None, None, 500),
            # 受限分析师：只能看美妆与家居
            ("analyst", "analyst123", Role.ANALYST, "分析师（限美妆/家居）",
             ["美妆", "家居"], None, 200),
            ("viewer", "viewer123", Role.VIEWER, "只读用户", None, None, 0),
        ]
        for username, pwd, role, name, cats, chans, quota in defaults:
            self._hashes[username] = hash_password(pwd)
            self._users[username] = User(
                username=username, role=role, display_name=name,
                scope=DataScope(cats, chans), daily_llm_quota=quota,
            )

    @property
    def using_defaults(self) -> bool:
        """是否在使用开发默认账号（健康检查会暴露这一点）。"""
        return not (os.getenv("EC_USERS", "").strip())

    def authenticate(self, username: str, password: str) -> User | None:
        """
        校验用户名密码。

        ★ 用户名不存在时也要做一次假的哈希校验，
          否则响应时间会泄露"该用户是否存在"（用户枚举攻击）。
        """
        user = self._users.get(username)
        stored = self._hashes.get(username, "")

        if user is None:
            verify_password(password, hash_password("dummy"))
            return None

        if not verify_password(password, stored):
            return None

        if user.disabled:
            return None

        return user

    def get(self, username: str) -> User | None:
        return self._users.get(username)

    def list_users(self) -> list[dict]:
        return [u.to_public() for u in self._users.values()]


# 全局单例
user_store = UserStore()


def get_jwt_secret() -> str:
    """
    获取 JWT 签名密钥。

    ★ 生产环境必须通过 EC_JWT_SECRET 注入。
      未配置时生成随机密钥（进程内有效）并打印警告 ——
      这样开发能跑，但重启后 token 失效，会立刻暴露"没配密钥"这件事，
      比悄悄用一个硬编码默认值安全得多。
    """
    secret = os.getenv("EC_JWT_SECRET", "").strip()
    if secret:
        return secret
    if not hasattr(get_jwt_secret, "_fallback"):
        get_jwt_secret._fallback = secrets.token_hex(32)   # type: ignore[attr-defined]
        print("[警告] 未配置 EC_JWT_SECRET，已生成临时密钥。"
              "重启后所有 Token 失效 —— 生产环境必须配置该环境变量。")
    return get_jwt_secret._fallback               # type: ignore[attr-defined]
