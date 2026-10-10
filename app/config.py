"""
app/config.py
=============
配置层：集中管理路径、模型名、Key。其他地方一律 `from app.config import settings`，
不要在业务代码里散落 os.getenv，否则以后换环境会到处漏改。
"""
from pathlib import Path

from dotenv import load_dotenv
import os

# 项目根目录（app/ 的上一级）
BASE_DIR = Path(__file__).resolve().parent.parent

# 读取 .env（不存在也不报错，方便 CI / 无 Key 跑纯数据链路）
load_dotenv(BASE_DIR / ".env")


class Settings:
    """全局配置对象。"""

    # --- LLM ---
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    LLM_MODEL: str = os.getenv("EC_LLM_MODEL", "deepseek-chat")
    LLM_MODEL_CHEAP: str = os.getenv("EC_LLM_MODEL_CHEAP", "") or os.getenv("EC_LLM_MODEL", "deepseek-chat")

    # --- 数据 ---
    DB_PATH: str = str(BASE_DIR / os.getenv("EC_DB_PATH", "data/ec.db"))

    # --- 护栏阈值（Guardrails）---
    # 单次调价幅度超过它，必须人工审批（此处先作为策略层判断依据）
    MAX_AUTO_PRICE_CHANGE_PCT: float = 10.0
    # 报表低于这个置信度就不许下结论，只输出"数据不足"
    MIN_CONFIDENCE_SAMPLE_DAYS: int = 7

    @property
    def has_api_key(self) -> bool:
        return bool(self.DEEPSEEK_API_KEY)


settings = Settings()
