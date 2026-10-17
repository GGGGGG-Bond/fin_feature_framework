"""SQLite 数据库连接管理与 schema 初始化。

使用 WAL 模式提升并发读写，开启外键约束。
只读连接（audit-raw 用）以 mode=ro 打开，不创建数据库、不改 journal_mode。
"""

import sqlite3
import urllib.parse
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


def get_readonly_connection(db_path: str) -> sqlite3.Connection:
    """只读连接：以 mode=ro 打开既有库，不创建数据库/目录、不改 journal_mode。

    库不存在时抛 OperationalError（不创建空库掩盖问题）；foreign_keys 是连接级
    PRAGMA，不持久化到库文件。供 audit-raw 等只读对账使用。
    """
    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise sqlite3.OperationalError(f"数据库不存在，只读连接拒绝创建：{path}")
    uri = "file:" + urllib.parse.quote(str(path), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_schema(db_path: str) -> None:
    """初始化所有表与索引。幂等操作，可重复执行。

    R1 修复：已有库且有待执行迁移时，先做一致性备份再迁移；备份失败在任何升级
    DDL 之前退出。已是最新版本的日常初始化不反复制造备份；空库直接迁移。
    """
    conn = get_connection(db_path)
    try:
        _init_schema_with_backup(conn, db_path)
        conn.commit()
    finally:
        conn.close()


def _init_schema_with_backup(conn: sqlite3.Connection, db_path: str) -> None:
    """升级入口：有待执行迁移且库非空时先备份，失败即不迁移。"""
    from fin_feature.storage.migrations import (
        MIGRATIONS,
        _database_has_existing_data,
        _get_current_version,
        backup_database_for_migration,
        default_backup_path,
    )

    current = _get_current_version(conn)
    latest = max(version for version, _, _ in MIGRATIONS)
    pending = current < latest
    populated = current > 0 or _database_has_existing_data(conn)
    if pending and populated:
        # 备份失败在此抛出，run_migrations 不会执行，原库保持不变
        backup_database_for_migration(conn, default_backup_path(db_path))
    run_migrations(conn)


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
