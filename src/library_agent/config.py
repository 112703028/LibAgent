from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["anthropic", "openai", "local"] = "openai"
    llm_model: str = "gpt-4o"           # 主模型（解析、書目探索）
    llm_model_mini: str = "gpt-4o-mini"  # 輕量呼叫（PDF 判斷、採購決議）
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    # OpenAI 相容閘道的 base URL（例如 myai168）；None = 用 OpenAI 官方
    openai_base_url: str | None = None

    # 用 127.0.0.1（IPv4）而非 localhost：Windows 上 localhost 可能解析到 IPv6 ::1，
    # 但 Docker 只綁 IPv4，會導致連線卡住。
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/library_agent"

    nccu_syllabus_base_url: str = "https://qrysub.nccu.edu.tw"
    crawler_politeness_delay_ms: int = 1500
    crawler_user_agent: str = "NCCU-Library-Agent/0.1"

    google_books_api_key: str | None = None
    # NCL / 全國圖書書目資訊網 NBINet 的 SRU（中文書存在性驗證，見 integrations/nla.py）
    ncl_sru_base_url: str = "https://nbinet.alma.exlibrisgroup.com/view/sru/886NCL_NBINET"

    alma_api_base_url: str = "https://api-ap.hosted.exlibrisgroup.com/almaws/v1"
    alma_api_key: str | None = None
    alma_z3950_host: str | None = None
    alma_z3950_port: int = 210
    alma_z3950_db: str | None = None

    log_level: str = "INFO"
    human_review_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)

    # 採購建議政策（recommender 用；由程式端確定性計算冊數）
    students_per_copy: int = 25    # 每幾位選課學生配 1 本
    max_purchase_copies: int = 5   # 單本書單次建議採購冊數上限


@lru_cache
def get_settings() -> Settings:
    return Settings()
