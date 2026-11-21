from __future__ import annotations
from app.db import connect
from app.services.clock import CLOCK, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
  meter_id TEXT PRIMARY KEY,
  building TEXT NOT NULL,
  unit_label TEXT NOT NULL,
  meter_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS periods (
  period_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'open',
  sealed_at TEXT,
  batch_no TEXT,
  sealed_clock TEXT,
  sealed_as_of TEXT
);
CREATE TABLE IF NOT EXISTS estimates (
  estimate_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS actuals (
  actual_id TEXT PRIMARY KEY,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  qty REAL NOT NULL,
  occurred_at TEXT NOT NULL,
  reader_code TEXT NOT NULL,
  doc_no TEXT NOT NULL,
  loaded_at TEXT NOT NULL,
  UNIQUE(meter_id, period_id, doc_no)
);
CREATE TABLE IF NOT EXISTS reconcile_batches (
  batch_id TEXT PRIMARY KEY,
  period_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  matched_count INTEGER NOT NULL DEFAULT 0,
  variance_count INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  created_at TEXT NOT NULL,
  -- 钟口径固化：本批按哪只钟、哪个 as_of 截止；封印后不可改
  clock TEXT NOT NULL DEFAULT 'loaded_at',
  as_of TEXT NOT NULL DEFAULT '',
  cutoff_at TEXT NOT NULL DEFAULT ''
);
-- 批次闭包快照：逐行固化 as_of 时刻纳入的估抄/实抄，封印后仍可回溯同一集合。
-- snapshot_id 标识整份快照（一批一份），(snapshot_id, meter_id) 为行主键
CREATE TABLE IF NOT EXISTS reconcile_snapshots (
  snapshot_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  meter_id TEXT NOT NULL,
  estimate_id TEXT,
  actual_id TEXT,
  estimate_qty REAL,
  actual_qty REAL,
  estimate_loaded_at TEXT,
  actual_loaded_at TEXT,
  actual_occurred_at TEXT,
  PRIMARY KEY (snapshot_id, meter_id)
);
CREATE TABLE IF NOT EXISTS variances (
  variance_id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  meter_id TEXT NOT NULL,
  period_id TEXT NOT NULL,
  estimate_qty REAL,
  actual_qty REAL,
  delta REAL NOT NULL,
  fate TEXT NOT NULL DEFAULT 'pending',
  -- 命运架构核心痕迹列：非 NULL 表示该差额已被后续对齐批取代，
  -- 既不再是现行差额，也不得进入任何未决视图（含其所属批的未决统计）。
  superseded_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# 已存在库的增量迁移（初始快照环境无 data/，此分支兜底旧库）
_COL_MIGRATIONS = {
    "periods": ["sealed_clock", "sealed_as_of"],
    "reconcile_batches": ["clock", "as_of", "cutoff_at"],
    "variances": ["superseded_at"],
}
_COL_DDL = {
    ("periods", "sealed_clock"): "ALTER TABLE periods ADD COLUMN sealed_clock TEXT",
    ("periods", "sealed_as_of"): "ALTER TABLE periods ADD COLUMN sealed_as_of TEXT",
    ("reconcile_batches", "clock"): "ALTER TABLE reconcile_batches ADD COLUMN clock TEXT NOT NULL DEFAULT 'loaded_at'",
    ("reconcile_batches", "as_of"): "ALTER TABLE reconcile_batches ADD COLUMN as_of TEXT NOT NULL DEFAULT ''",
    ("reconcile_batches", "cutoff_at"): "ALTER TABLE reconcile_batches ADD COLUMN cutoff_at TEXT NOT NULL DEFAULT ''",
    ("variances", "superseded_at"): "ALTER TABLE variances ADD COLUMN superseded_at TEXT",
}


def _existing_columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _heal_legacy_tears(conn) -> None:
    """收敛旧版本遗留的重跑撕裂，使库落到命运架构不变量上。

    旧病：重跑对齐不取代旧批，同 (period_id, meter_id) 可在多个 open 批
    各挂一条现行差额；旧批不标 superseded，总览把多批未决加总。
    收敛口径（与运行时 supersede_live 完全一致）：
    - 每账期只保留最新一个 open 批为现行，更早的 open 批标 superseded；
    - 同键多条现行差额只保留最新一条，其余打 superseded_at 留痕；
    - sealed 批与其差额原样保留，封印期事实不被迁移改写。
    幂等：已收敛的库再跑一次不产生变化。
    """
    stamp = now_iso()
    open_batches = conn.execute(
        "SELECT batch_id, period_id, created_at FROM reconcile_batches "
        "WHERE status='open' ORDER BY period_id, created_at DESC, batch_id DESC"
    ).fetchall()
    seen_periods: set[str] = set()
    for r in open_batches:
        if r["period_id"] in seen_periods:
            conn.execute(
                "UPDATE reconcile_batches SET status='superseded' WHERE batch_id=?",
                (r["batch_id"],),
            )
            conn.execute(
                "UPDATE variances SET superseded_at=? "
                "WHERE batch_id=? AND superseded_at IS NULL",
                (stamp, r["batch_id"]),
            )
        else:
            seen_periods.add(r["period_id"])
    # 即便批次状态已收敛，同键仍可能残留多条现行差额（旧数据的双未决）
    dupes = conn.execute(
        """SELECT period_id, meter_id FROM variances
           WHERE superseded_at IS NULL
           GROUP BY period_id, meter_id HAVING COUNT(*) > 1"""
    ).fetchall()
    for d in dupes:
        rows = conn.execute(
            """SELECT variance_id FROM variances
               WHERE period_id=? AND meter_id=? AND superseded_at IS NULL
               ORDER BY rowid DESC""",
            (d["period_id"], d["meter_id"]),
        ).fetchall()
        for old in rows[1:]:
            conn.execute(
                "UPDATE variances SET superseded_at=? WHERE variance_id=?",
                (stamp, old["variance_id"]),
            )


def migrate(conn) -> None:
    conn.executescript(SCHEMA)
    for table, cols in _COL_MIGRATIONS.items():
        present = _existing_columns(conn, table)
        for col in cols:
            if col not in present:
                conn.execute(_COL_DDL[(table, col)])
    # 旧批次若缺钟口径，统一补装载钟，杜绝无钟批次
    conn.execute(
        "UPDATE reconcile_batches SET clock=? WHERE clock IS NULL OR clock=''",
        (CLOCK,),
    )
    # 先收敛旧撕裂，再建现行唯一索引；否则历史双现行行会让建索引失败
    _heal_legacy_tears(conn)
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS ux_variances_live
           ON variances(period_id, meter_id) WHERE superseded_at IS NULL"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ix_variances_live_pending
           ON variances(period_id)
           WHERE superseded_at IS NULL AND fate='pending'"""
    )
    conn.commit()


def seed_if_empty() -> None:
    conn = connect()
    try:
        migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        if n:
            return
        meters = [
            ("M-W-101", "A栋", "101", "water"),
            ("M-W-102", "A栋", "102", "water"),
            ("M-E-101", "A栋", "101", "electric"),
            ("M-W-201", "B栋", "201", "water"),
            ("M-E-201", "B栋", "201", "electric"),
            ("M-E-202", "B栋", "202", "electric"),
        ]
        conn.executemany(
            "INSERT INTO meters(meter_id,building,unit_label,meter_type,status) VALUES (?,?,?,?, 'active')",
            meters,
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-10','open')"
        )
        conn.execute(
            "INSERT INTO periods(period_id,status) VALUES ('2026-09','open')"
        )
        # estimates for 2026-10
        est = [
            ("E1", "M-W-101", "2026-10", 12.0, "EST-101", "2026-10-01T08:00:00"),
            ("E2", "M-W-102", "2026-10", 15.0, "EST-102", "2026-10-01T08:00:00"),
            ("E3", "M-E-101", "2026-10", 220.0, "EST-E101", "2026-10-01T08:00:00"),
            ("E4", "M-W-201", "2026-10", 18.0, "EST-201", "2026-10-01T08:00:00"),
            ("E5", "M-E-201", "2026-10", 310.0, "EST-E201", "2026-10-01T08:00:00"),
            ("E6", "M-E-202", "2026-10", 280.0, "EST-E202", "2026-10-01T08:00:00"),
        ]
        conn.executemany(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) VALUES (?,?,?,?,?,?)",
            est,
        )
        # actuals: most match; two variance
        act = [
            ("A1", "M-W-101", "2026-10", 12.0, "2026-10-28T10:00:00", "R01", "ACT-101", "2026-10-28T10:05:00"),
            ("A2", "M-W-102", "2026-10", 14.0, "2026-10-28T10:10:00", "R01", "ACT-102", "2026-10-28T10:15:00"),  # short
            ("A3", "M-E-101", "2026-10", 220.0, "2026-10-28T11:00:00", "R02", "ACT-E101", "2026-10-28T11:05:00"),
            ("A4", "M-W-201", "2026-10", 18.0, "2026-10-28T11:20:00", "R02", "ACT-201", "2026-10-28T11:25:00"),
            ("A5", "M-E-201", "2026-10", 330.0, "2026-10-28T12:00:00", "R03", "ACT-E201", "2026-10-28T12:05:00"),  # over
            ("A6", "M-E-202", "2026-10", 280.0, "2026-10-28T12:20:00", "R03", "ACT-E202", "2026-10-28T12:25:00"),
        ]
        conn.executemany(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at) VALUES (?,?,?,?,?,?,?,?)",
            act,
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('current_period','2026-10')"
        )
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('product_title','抄表簿')"
        )
        # 界面不露英文：表类型仍存 water/electric 码，前端映射为中文
        conn.commit()
    finally:
        conn.close()
