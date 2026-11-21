"""测试辅助：固定装载钟时刻写入正式行，断言查询集中化。"""
from __future__ import annotations

from app.db import connect

AS_OF = "2026-10-31T00:00:00+00:00"
LOAD = "2026-10-01T08:00:00+00:00"


def load_estimate(mid: str, period: str, qty: float, doc: str, loaded_at: str = LOAD) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"E-{doc}", mid, period, qty, f"DOC-{doc}", loaded_at),
        )
        conn.commit()
    finally:
        conn.close()


def load_actual(mid: str, period: str, qty: float, doc: str, loaded_at: str = LOAD) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,"
            "reader_code,doc_no,loaded_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"A-{doc}", mid, period, qty, "2026-10-28T10:00:00+00:00",
             "R01", f"DOC-{doc}", loaded_at),
        )
        conn.commit()
    finally:
        conn.close()


def query_all(sql: str, params=()):
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def query_one(sql: str, params=()):
    conn = connect()
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def batch_statuses(period: str) -> dict[str, str]:
    rows = query_all(
        "SELECT batch_id, status FROM reconcile_batches WHERE period_id=?", (period,)
    )
    return {r["batch_id"]: r["status"] for r in rows}


def live_variance(period: str, mid: str):
    return query_one(
        "SELECT * FROM variances WHERE period_id=? AND meter_id=? AND superseded_at IS NULL",
        (period, mid),
    )


def all_variance_rows(period: str, mid: str):
    return query_all(
        "SELECT * FROM variances WHERE period_id=? AND meter_id=? ORDER BY rowid",
        (period, mid),
    )


def count(what: str, params=()) -> int:
    return query_one(f"SELECT COUNT(*) AS c FROM {what}", params)["c"]
