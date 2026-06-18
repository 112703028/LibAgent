from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from library_agent.config import get_settings

_settings = get_settings()

# engine 是跟 PostgreSQL 的連線池，整個程式共用一個
engine = create_engine(_settings.database_url, echo=False)

# SessionLocal 是工廠，每次要操作 DB 時呼叫 SessionLocal() 產生一個 session
# autocommit=False：不自動提交，你要手動 session.commit() 才會寫入 DB
# autoflush=False：不自動同步，避免意外的 SQL 在你不預期的時機跑出來
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
