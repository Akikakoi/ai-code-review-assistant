"""数据库连接。

对应开发文档 §5.1 / §13.2。

`DATABASE_URL` 留空时自动降级为 SQLite（落在 `$ACRA_WORKDIR/acra.db`），
这样阶段一的 CLI 不依赖 Docker 就能跑完整链路；填了 PostgreSQL 就走生产形态。
方言差异（JSONB / ARRAY）在 `store/models.py` 里用 `with_variant` 处理，
业务代码不需要分支。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def normalize_url(database_url: str) -> str:
    """把 `postgresql://` 归一化为 psycopg3 驱动 URL。"""
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def make_engine(database_url: str | None, workdir: Path) -> Engine:
    url = normalize_url(database_url) if database_url else f"sqlite:///{(workdir / 'acra.db').as_posix()}"

    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # pragma: no cover
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


class Database:
    """引擎 + 会话工厂的轻量封装。"""

    def __init__(self, database_url: str | None, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.engine = make_engine(database_url, workdir)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    def create_all(self) -> None:
        from acra.store.models import Base

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
