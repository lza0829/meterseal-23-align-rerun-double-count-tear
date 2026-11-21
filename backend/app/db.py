from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH, SQLITE_SYNCHRONOUS


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # WAL：读写不互斥到文件锁级别；配合 busy_timeout，
    # 对齐 BEGIN IMMEDIATE 持锁期间的并发装载排队等待，而非直接失败
    conn.execute("PRAGMA journal_mode=WAL")
    if SQLITE_SYNCHRONOUS in ("OFF", "NORMAL", "FULL", "EXTRA"):
        conn.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS}")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
