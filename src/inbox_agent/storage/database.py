"""Database construction and transaction helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


def sqlite_url(path: Path) -> str:
    """Return an absolute SQLAlchemy URL for a SQLite file."""

    return f"sqlite:///{path.resolve().as_posix()}"


def create_sqlite_engine(path: Path) -> Engine:
    """Create a local engine without creating or migrating the database."""

    engine = create_engine(
        sqlite_url(path),
        future=True,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_sqlite_safety(dbapi_connection: object, _: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


class Database:
    """Small explicit wrapper around the SQLAlchemy engine and sessions."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.engine = create_sqlite_engine(self.path)
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Commit one unit of work, rolling back on every exception."""

        with self._session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def dispose(self) -> None:
        """Release open connection-pool resources."""

        self.engine.dispose()
