from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    llm_provider: Literal["anthropic", "openai", "local"] = "anthropic"
    llm_model: str = "claude-opus-4-7"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/library_agent"

    nccu_syllabus_base_url: str = "https://qrysub.nccu.edu.tw"
    crawler_politeness_delay_ms: int = 1500
    crawler_user_agent: str = "NCCU-Library-Agent/0.1"

    google_books_api_key: str | None = None
    nla_open_data_base_url: str = "https://openapi.ncl.edu.tw"

    alma_api_base_url: str = "https://api-ap.hosted.exlibrisgroup.com/almaws/v1"
    alma_api_key: str | None = None
    alma_z3950_host: str | None = None
    alma_z3950_port: int = 210
    alma_z3950_db: str | None = None

    log_level: str = "INFO"
    human_review_confidence_threshold: float = Field(default=0.6, ge=0.0, le=1.0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
