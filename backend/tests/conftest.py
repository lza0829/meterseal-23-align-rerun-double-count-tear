"""测试夹具：每个用例一个隔离临时库，绝不碰工作区 data/。

直接驱动服务层 + TestClient 双入口；库结构经 seed.migrate 真实迁移建立，
与生产同一只 schema、同一套索引。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import connect
from app.seed import migrate


@pytest.fixture()
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    # app.db 在导入时绑定了 DB_PATH，必须同步改模块属性
    from app import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)

    conn = connect()
    migrate(conn)
    # 最小主数据：两个账期、三只表
    conn.executemany(
        "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) "
        "VALUES (?,?,?,?,'active')",
        [("M1", "A栋", "1", "water"), ("M2", "A栋", "2", "water"),
         ("M3", "B栋", "1", "electric")],
    )
    conn.executemany(
        "INSERT INTO periods(period_id,status) VALUES (?, 'open')",
        [("P1",), ("P2",)],
    )
    conn.commit()
    conn.close()
    yield db_mod
