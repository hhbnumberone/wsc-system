# -*- coding: utf-8 -*-
"""SQLite 连接与事务的公共实现。

终端本地库和平台库都用这一份 —— 两边的 WAL、busy_timeout、
按线程持有连接、显式事务的写法必须一致，否则会出现
"终端能跑、平台锁库"这类只在压力下暴露的问题。

本机 SQLite 3.50.4，`sqlite3.threadsafety == 3`（serialized）。
即便如此仍按线程分开连接：一是避免跨线程共享连接导致
`ProgrammingError`，二是让每个线程有独立的事务边界。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any, Iterable, Iterator

#: 锁等待。页面轮询（读）和上传入库（写）并发时，等 5 秒足够，
#: 等不到说明有事务开太久，那本来就该报错而不是无限等。
BUSY_TIMEOUT_MS = 5000


def connect(path: str, *, timeout: float = 5.0) -> sqlite3.Connection:
    """新建连接并设好 pragma。调用方负责关闭（或用 ThreadLocalConns）。"""
    p = os.path.abspath(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    conn = sqlite3.connect(p, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")     # 读写不互相阻塞
    cur.execute("PRAGMA synchronous=NORMAL")   # WAL 下足够安全，写入快很多
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
    return conn


class ThreadLocalConns:
    """按线程持有连接。

    用法：
        conns = ThreadLocalConns()
        conn = conns.get("/path/to.db")
        ...
        conns.close()          # 线程退出前调用
    """

    def __init__(self, name: str = "sqlite"):
        self._local = threading.local()
        self._name = name

    def get(self, path: str) -> sqlite3.Connection:
        p = os.path.abspath(path)
        conn = getattr(self._local, "conn", None)
        if conn is not None and getattr(self._local, "path", None) == p:
            return conn
        if conn is not None:
            # 换了库：关掉旧的，免得两个库的连接同时挂着
            try:
                conn.close()
            except sqlite3.Error:
                pass
        conn = connect(p)
        self._local.conn = conn
        self._local.path = p
        return conn

    def close(self) -> None:
        """关闭当前线程的连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
            self._local.path = None

    # 让 ThreadLocalConns 支持 with 语句，退出时自动关闭
    def __enter__(self) -> "ThreadLocalConns":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


class transaction:
    """显式事务。

    连接是 autocommit 模式（`isolation_level=None`），必须手动 BEGIN/COMMIT。
    用 `BEGIN IMMEDIATE` 而不是默认的 deferred：一开始就拿写锁，
    避免"读的时候没事、写的时候才发现别人先提交了"导致的
    `SQLITE_BUSY_SNAPSHOT`（deferred 事务升级锁时会直接失败，不重试）。

    嵌套调用是安全的：内层发现已在事务里就不再 BEGIN。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.owns = not conn.in_transaction

    def __enter__(self) -> sqlite3.Connection:
        if self.owns:
            self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.owns:
            return False
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover - 回滚失败说明连接已坏
                pass
        return False


def insert_many(conn: sqlite3.Connection, table: str, columns: Iterable[str],
                rows: Iterable[tuple], *, or_ignore: bool = True) -> int:
    """批量插入，返回**实际写入**的行数。

    `or_ignore=True` 走 `INSERT OR IGNORE` —— 这是幂等的最后一道保险：
    即便上层判重有漏洞，主键冲突也不会变成异常，只是这一行不生效。
    用 `total_changes` 的差值算真实写入数，不靠"我传了几行"来推断。
    """
    cols = list(columns)
    data = list(rows)
    if not data:
        return 0
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    sql = (f"{verb} INTO {table} ({','.join(cols)})"
           f" VALUES ({','.join('?' * len(cols))})")
    before = conn.total_changes
    conn.executemany(sql, data)
    return conn.total_changes - before


def migrate(conn: sqlite3.Connection,
            columns: Iterable[tuple[str, str, str]]) -> list[str]:
    """给已存在的表补列。

    `CREATE TABLE IF NOT EXISTS` 不会给老表加新列，而演示机上可能已经跑过一次。
    `columns` 是 (表名, 列名, 列定义) 的序列。返回实际添加的 "表.列" 列表。
    """
    added: list[str] = []
    for table, column, decl in columns:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not info:                    # 表还不存在，建表语句里已经带了这一列
            continue
        if column in {r["name"] for r in info}:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        added.append(f"{table}.{column}")
    return added


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """各表行数，用于启动自检和演示前检查。"""
    out: dict[str, int] = {}
    cur = conn.cursor()
    for (name,) in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
        try:
            out[name] = cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        except sqlite3.Error:
            out[name] = -1
    cur.close()
    return out


def scalar(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = (),
           default: Any = 0) -> Any:
    """取单个标量值，查不到时返回 default。"""
    row = conn.execute(sql, tuple(args)).fetchone()
    if row is None:
        return default
    v = row[0]
    return default if v is None else v


def chunked(items: list, size: int = 400) -> Iterator[list]:
    """把列表切成小块。SQLite 的变量数有上限（默认 999），
    拼 `IN (...)` 时必须分块，否则记录一多就报
    "too many SQL variables"。"""
    for i in range(0, len(items), size):
        yield items[i:i + size]
