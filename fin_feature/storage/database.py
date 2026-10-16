"""SQLite 数据库连接管理与 schema 初始化。

使用 WAL 模式提升并发读写，开启外键约束。
"""

import sqlite3
from pathlib import Path

from fin_feature.storage.migrations import run_migrations


def get_connection(db_path: str) -> sqlite3.Connection:
    """获取 SQLite 连接，开启 WAL 模式与外键约束。

    连接的 row_factory 设为 sqlite3.Row，使查询结果可按列名访问。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(db_path: str) -> None:
    """初始化所有表与索引。幂等操作，可重复执行。

    使用迁移框架 run_migrations 替代 ad-hoc 建表，确保 schema_version 追踪。
    """
    conn = get_connection(db_path)
    try:
        run_migrations(conn)
        conn.commit()
    finally:
        conn.close()


class Database:
    """数据库连接的上下文管理器封装，便于在模块间共享连接。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = get_connection(self.db_path)
        return self._conn

    def init(self) -> None:
        """初始化 schema（建表 + 索引）。"""
        init_schema(self.db_path)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
