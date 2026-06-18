import sys
from pathlib import Path
from logging.config import fileConfig

# engine_from_config：從設定檔建立 DB 連線引擎
# pool：連線池設定，這裡用 NullPool（migration 不需要連線池）
# context：Alembic 的核心物件，控制整個 migration 流程
from sqlalchemy import engine_from_config, pool
from alembic import context

# 讓 alembic 找得到 src/library_agent
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from library_agent.config import get_settings
from library_agent.db.models import Base

config = context.config  # 代表 alembic.ini 的內容（Alembic 自動載入）
if config.config_file_name is not None:  # config.config_file_name 是 alembic.ini 的檔案路徑
    fileConfig(config.config_file_name)   # 讀取 alembic.ini 裡的 logging 設定，讓我們在 migration 腳本裡也能用 logging 模組寫 log

# 告訴 Alembic 要比對哪些 model 來產生 migration
target_metadata = Base.metadata

# 從 .env 讀連線字串，在執行期動態修改 alembic.ini 的設定值
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), # 讀取 alembic.ini 裡 [alembic] 那個 section 的所有設定
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    '''
    connectable.connect()：建立一條真實的 DB 連線
    with ... as connection：用完自動關閉連線（Python context manager）
    context.configure：把連線和 metadata 都綁進 Alembic context
    begin_transaction + run_migrations：在一個 transaction 裡跑所有待執行的 migration，失敗自動 rollback
    '''
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
