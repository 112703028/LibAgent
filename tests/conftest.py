import os

# 讓 import 時會建構 OpenAI client 的模組（parser/discoverer/recommender）不會因缺 key 而爆。
# 這些測試不會真的呼叫任何外部 API。
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key")


# ---------------------------------------------------------------------------
# 獨立的測試資料庫
#
# recommender 永遠以「全校」資料計算並清掉不再存在的建議，如果在開發資料庫上測試，
# 會直接改寫真實資料。需要這種全域操作的測試用 isolated_db：另建一個 `<開發庫>_test`
# 資料庫，由 models 建表，每個測試前清空。其他測試仍沿用「在開發庫播種專用資料、事後清掉」。
# ---------------------------------------------------------------------------
import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture(scope="session")
def _isolated_engine():
    from library_agent.config import get_settings
    from library_agent.db.models import Base

    url = make_url(get_settings().database_url)
    test_db = f"{url.database}_test"
    # 硬性保險：不論設定怎麼寫，這個引擎絕不可能指到開發資料庫
    assert test_db.endswith("_test") and test_db != url.database

    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("select 1 from pg_database where datname = :n"), {"n": test_db}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{test_db}"'))
    admin.dispose()

    engine = create_engine(url.set(database=test_db))
    assert engine.url.database == test_db
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def isolated_db(_isolated_engine):
    """回傳綁在獨立測試資料庫的 sessionmaker；每個測試前先清空所有表。"""
    from library_agent.db.models import Base

    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with _isolated_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return sessionmaker(bind=_isolated_engine, autocommit=False, autoflush=False)
