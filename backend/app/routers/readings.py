from __future__ import annotations
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import connect
from app.services.gate import assert_period_writable
from app.services.clock import now_iso

router = APIRouter(tags=["readings"])


class EstimateIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    doc_no: str = Field(min_length=1)


class ActualIn(BaseModel):
    meter_id: str
    period_id: str
    qty: float
    occurred_at: str
    reader_code: str = "R00"
    doc_no: str = Field(min_length=1)


def _begin_writable(conn, period_id: str):
    """拿 IMMEDIATE 写锁并在锁内复查封印状态。

    与对齐/封印事务互斥：要么整笔装载先于对齐完成并被闭包纳入，
    要么晚于对齐、落在其 as_of 截止面之外，不存在半截混钟。
    """
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT status FROM periods WHERE period_id=?", (period_id,)
    ).fetchone()
    if not row:
        conn.execute("ROLLBACK")
        raise HTTPException(404, f"账期不存在: {period_id}")
    if row["status"] == "sealed":
        conn.execute("ROLLBACK")
        raise HTTPException(409, f"账期已封印，禁止写入: {period_id}")


@router.get("/estimates")
def list_estimates(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM estimates WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM estimates ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/estimates")
def create_estimate(body: EstimateIn):
    assert_period_writable(body.period_id)  # 快速失败；锁内还会复查
    conn = connect()
    try:
        _begin_writable(conn, body.period_id)
        mid = conn.execute("SELECT 1 FROM meters WHERE meter_id=?", (body.meter_id,)).fetchone()
        if not mid:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "表具不存在")
        eid = "E-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO estimates(estimate_id,meter_id,period_id,qty,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?)""",
                (eid, body.meter_id, body.period_id, body.qty, body.doc_no, now_iso()),
            )
            conn.commit()
        except Exception as e:
            conn.execute("ROLLBACK")
            raise HTTPException(409, f"估抄写入失败: {e}") from e
        return {"estimate_id": eid, "ok": True}
    finally:
        conn.close()


@router.get("/actuals")
def list_actuals(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM actuals WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM actuals ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/actuals")
def create_actual(body: ActualIn):
    assert_period_writable(body.period_id)  # 快速失败；锁内还会复查
    conn = connect()
    try:
        _begin_writable(conn, body.period_id)
        mid = conn.execute("SELECT 1 FROM meters WHERE meter_id=?", (body.meter_id,)).fetchone()
        if not mid:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "表具不存在")
        aid = "A-" + uuid.uuid4().hex[:10]
        try:
            conn.execute(
                """INSERT INTO actuals(actual_id,meter_id,period_id,qty,occurred_at,reader_code,doc_no,loaded_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    body.meter_id,
                    body.period_id,
                    body.qty,
                    body.occurred_at,
                    body.reader_code,
                    body.doc_no,
                    now_iso(),
                ),
            )
            conn.commit()
        except Exception as e:
            conn.execute("ROLLBACK")
            raise HTTPException(409, f"实抄写入失败: {e}") from e
        return {"actual_id": aid, "ok": True}
    finally:
        conn.close()
