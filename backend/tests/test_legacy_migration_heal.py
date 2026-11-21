"""旧版本撕裂库的迁移收敛：迁移本身必须把库修到命运架构不变量上。

旧病构造（无 superseded_at 列的旧 schema）：
- 同账期两个 open 批，同键各挂一条 pending（双未决、双现行 open）；
- 旧软取代是空操作，superseded 状态从未被写入。
"""
from __future__ import annotations

import pytest

from app import config
from app.db import connect
from app.seed import migrate
from app.services import batch_rollup, reconcile_service
from tests.helpers import AS_OF, query_all

LEGACY_SCHEMA = """
CREATE TABLE meters (
  meter_id TEXT PRIMARY KEY, building TEXT NOT NULL, unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE periods (
  period_id TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT, batch_no TEXT, sealed_clock TEXT, sealed_as_of TEXT
);
CREATE TABLE estimates (
  estimate_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, period_id TEXT NOT NULL,
  qty REAL NOT NULL, doc_no TEXT NOT NULL, loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE actuals (
  actual_id TEXT PRIMARY KEY, meter_id TEXT NOT NULL, period_id TEXT NOT NULL,
  qty REAL NOT NULL, occurred_at TEXT NOT NULL, reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL, loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE reconcile_batches (
  batch_id TEXT PRIMARY KEY, period_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0, variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT, created_at TEXT NOT NULL,
  clock TEXT NOT NULL DEFAULT 'loaded_at', as_of TEXT NOT NULL DEFAULT '',
  cutoff_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE reconcile_snapshots (
  snapshot_id TEXT NOT NULL, batch_id TEXT NOT NULL, meter_id TEXT NOT NULL,
  estimate_id TEXT, actual_id TEXT, estimate_qty REAL, actual_qty REAL,
  estimate_loaded_at TEXT, actual_loaded_at TEXT, actual_occurred_at TEXT,
  PRIMARY KEY (snapshot_id, meter_id)
);
CREATE TABLE variances (
  variance_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL, estimate_qty REAL, actual_qty REAL, delta REAL NOT NULL,
  fate TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _build_legacy_db(path) -> None:
    conn = connect()
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) "
            "VALUES ('M1','A','1','water','active')"
        )
        conn.execute("INSERT INTO periods(period_id,status) VALUES ('P1','open')")
        # 两个 open 批：同键 M1 各一条 pending（重跑双未决撕裂）
        conn.execute(
            "INSERT INTO reconcile_batches(batch_id,period_id,status,variance_count,"
            "created_at,clock,as_of,cutoff_at) VALUES "
            "('B-OLD-1','P1','open',1,'2026-10-01T00:00:00+00:00','loaded_at',?,?)",
            (AS_OF, AS_OF),
        )
        conn.execute(
            "INSERT INTO reconcile_batches(batch_id,period_id,status,variance_count,"
            "created_at,clock,as_of,cutoff_at) VALUES "
            "('B-OLD-2','P1','open',1,'2026-10-02T00:00:00+00:00','loaded_at',?,?)",
            (AS_OF, AS_OF),
        )
        conn.execute(
            "INSERT INTO variances(variance_id,batch_id,meter_id,period_id,delta,fate) "
            "VALUES ('V-OLD-1','B-OLD-1','M1','P1',-2.0,'pending')"
        )
        conn.execute(
            "INSERT INTO variances(variance_id,batch_id,meter_id,period_id,delta,fate) "
            "VALUES ('V-OLD-2','B-OLD-2','M1','P1',-2.0,'pending')"
        )
        conn.commit()
    finally:
        conn.close()


def test_migration_heals_legacy_tears(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from app import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "DATA_DIR", tmp_path)

    _build_legacy_db(db_path)

    conn = connect()
    try:
        migrate(conn)  # 触发列迁移 + 撕裂收敛 + 唯一索引建立
        # 1) 旧批只剩一个 open，更早的被标 superseded
        statuses = {
            r["batch_id"]: r["status"]
            for r in conn.execute(
                "SELECT batch_id, status FROM reconcile_batches WHERE period_id='P1'"
            ).fetchall()
        }
        assert statuses == {"B-OLD-1": "superseded", "B-OLD-2": "open"}

        # 2) 同键只留一条现行，旧行打痕迹且行不丢（可审计）
        rows = conn.execute(
            "SELECT variance_id, superseded_at FROM variances "
            "WHERE period_id='P1' AND meter_id='M1' ORDER BY rowid"
        ).fetchall()
        assert [r["variance_id"] for r in rows] == ["V-OLD-1", "V-OLD-2"]
        assert rows[0]["superseded_at"] is not None
        assert rows[1]["superseded_at"] is None

        # 3) 现行未决计数不再双计
        assert batch_rollup.live_pending_count(conn, "P1") == 1

        # 4) 唯一索引真实存在且生效
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO variances(variance_id,batch_id,meter_id,period_id,"
                "delta,fate,superseded_at) VALUES "
                "('VX','B-OLD-2','M1','P1',1.0,'pending',NULL)"
            )
        conn.rollback()

        # 5) 迁移幂等：再跑一次状态不变
        migrate(conn)
        again = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE superseded_at IS NULL"
        ).fetchone()["c"]
        assert again == 1
    finally:
        conn.close()

    # 6) 收敛后的库可直接承接重跑对齐，不产生双现行
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) "
            "VALUES ('E1','M1','P1',12.0,'D1','2026-10-01T08:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,"
            "reader_code,doc_no,loaded_at) VALUES "
            "('A1','M1','P1',10.0,'2026-10-28T10:00:00+00:00','R1','D1',"
            "'2026-10-01T08:00:00+00:00')"
        )
        conn.commit()
    finally:
        conn.close()
    out = reconcile_service.run_align("P1", AS_OF)
    opens = query_all(
        "SELECT batch_id FROM reconcile_batches WHERE period_id='P1' AND status='open'"
    )
    assert [r["batch_id"] for r in opens] == [out["batch_id"]]
