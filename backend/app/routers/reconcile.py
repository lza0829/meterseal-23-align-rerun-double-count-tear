from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.services import batch_rollup

from app.db import connect
from app.services import reconcile_service
from app.services.clock import CLOCK

router = APIRouter(tags=["reconcile"])


class AlignIn(BaseModel):
    period_id: str
    as_of: str | None = None
    clock: str | None = None


class SealIn(BaseModel):
    period_id: str
    batch_id: str | None = None
    # 可选：须与对齐批次固化的 as_of 完全一致，否则 409
    as_of: str | None = None
    clock: str | None = None


class FateIn(BaseModel):
    fate: str


@router.post("/align")
def align(body: AlignIn):
    return reconcile_service.run_align(body.period_id, body.as_of, body.clock)


@router.get("/clock")
def clock_policy():
    """全局钟口径回显：前端与审计方据此确认只有一套真。"""
    return {"clock": CLOCK, "audit_only": "occurred_at"}


@router.get("/batches")
def batches(period_id: str | None = None):
    conn = connect()
    try:
        if period_id:
            rows = conn.execute(
                "SELECT * FROM reconcile_batches WHERE period_id=? ORDER BY created_at DESC",
                (period_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(batch_id: str):
    """回溯某批次落封/预览时的闭包快照（含 occurred_at 审计列）。"""
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT clock, as_of FROM reconcile_batches WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        rows = conn.execute(
            "SELECT * FROM reconcile_snapshots WHERE batch_id=? ORDER BY meter_id",
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id,
            "clock": batch["clock"],
            "as_of": batch["as_of"],
            "rows": [dict(r) for r in rows],
        }
    finally:
        conn.close()


@router.get("/variances")
def variances(batch_id: str | None = None, period_id: str | None = None):
    conn = connect()
    try:
        if batch_id:
            rows = conn.execute(
                "SELECT * FROM variances WHERE batch_id=? ORDER BY meter_id", (batch_id,)
            ).fetchall()
        elif period_id:
            rows = conn.execute(
                "SELECT * FROM variances WHERE period_id=? ORDER BY meter_id", (period_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM variances ORDER BY period_id, meter_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/variances/{variance_id}/fate")
def set_fate(variance_id: str, body: FateIn):
    return reconcile_service.set_fate(variance_id, body.fate)


@router.post("/seal")
def seal(body: SealIn):
    return reconcile_service.seal_period(
        body.period_id, body.batch_id, body.as_of, body.clock
    )


@router.get("/periods")
def periods():
    conn = connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM periods ORDER BY period_id DESC").fetchall()]
    finally:
        conn.close()


@router.get("/rollup")
def rollup(period_id: str):
    conn = connect()
    try:
        return batch_rollup.rollup_report(conn, period_id)
    finally:
        conn.close()
