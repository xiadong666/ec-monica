# ==========================================================================
# EC-Monica 生产镜像
#
# 设计要点：
#   ① 多阶段构建 —— 构建依赖不进入运行镜像，减小体积与攻击面
#   ② 非 root 用户运行 —— 容器逃逸后的权限最小化
#   ③ 健康检查 —— 编排系统可据此判断实例是否可用
#   ④ 不打包 .env / 测试数据 —— 配置由环境变量注入，见 compose
# ==========================================================================

# ---------- 阶段一：依赖构建 ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# 只拷贝依赖清单，先装依赖再拷代码 ——
# 这样改代码不会让依赖层缓存失效，构建更快
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ---------- 阶段二：运行镜像 ----------
FROM python:3.11-slim

# 安全：创建一个非 root 用户
RUN groupadd --gid 10001 ecmonica \
    && useradd --uid 10001 --gid ecmonica --shell /bin/false --create-home ecmonica

# curl 仅用于 HEALTHCHECK
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 从构建阶段拷贝已安装的依赖
COPY --from=builder /install /usr/local

# 拷贝应用代码
COPY app/ ./app/
COPY scripts/ ./scripts/

# 数据目录（SQLite 文件或挂载点），属主给非 root 用户
RUN mkdir -p /app/data /app/docs \
    && chown -R ecmonica:ecmonica /app

USER ecmonica

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EC_DB_PATH=/app/data/ec.db \
    EC_SESSION_BACKEND=memory \
    EC_LOG_LEVEL=INFO

EXPOSE 8000

# 健康检查：走不需要认证的 /api/health
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

# 生产用多 worker；--proxy-headers 让日志里的客户端 IP 正确
# 注意：多 worker 时必须把 EC_SESSION_BACKEND 设为 redis，
#      否则每个 worker 各存一份会话（见 app/session.py 的说明）
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "2", \
     "--proxy-headers", \
     "--access-log"]
