"""SQLite 数据库连接管理与 schema 初始化。

使用 WAL 模式提升并发读写，开启外键约束。
"""

import sqlite3
from pathlib import Path

from fin_feature.storage import schema


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
    """初始化所有表与索引。幂等操作，可重复执行。"""
    conn = get_connection(db_path)
    try:
        for ddl in schema.ALL_DDL:
            conn.execute(ddl)
        for idx in schema.ALL_INDEXES:
            conn.execute(idx)
        # 幂等迁移：为已有 features 表补充 definition_version 列
        _migrate_features_definition_version(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate_features_definition_version(conn: sqlite3.Connection) -> None:
    """为已有 features 表幂等添加 definition_version 列。

    旧库创建时无此列；新库 DDL 已包含。检查列是否存在，不存在则 ALTER TABLE。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)").fetchall()}
    if "definition_version" not in cols:
        conn.execute(
            "ALTER TABLE features ADD COLUMN definition_version TEXT"
        )


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
