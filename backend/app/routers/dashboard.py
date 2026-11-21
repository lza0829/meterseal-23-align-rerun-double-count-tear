from __future__ import annotations
from fastapi import APIRouter
from app.db import connect
from app.services.gate import get_setting
from app.services.clock import CLOCK, AUDIT_CLOCK
from app.services import batch_rollup

router = APIRouter(tags=["dashboard"])


@router.get("/health")
def health():
    return {"ok": True, "product": "抄表簿"}


@router.get("/overview")
def overview():
    conn = connect()
    try:
        open_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='open'"
        ).fetchone()["c"]
        sealed_periods = conn.execute(
            "SELECT COUNT(*) AS c FROM periods WHERE status='sealed'"
        ).fetchone()["c"]
        meters = conn.execute("SELECT COUNT(*) AS c FROM meters").fetchone()["c"]
        current = get_setting("current_period", "")
        # 最近批次带出 clock/as_of，总览与对齐页看到的是同一套钟口径
        batches = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM reconcile_batches ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        ]
        # 未决只数现行 pending（superseded_at IS NULL）：
        # 旧批 superseded 后不可能再驱动此计数，禁止多批加总
        pending = batch_rollup.live_pending_count(conn)
        return {
            "clock": CLOCK,
            "audit_only_clock": AUDIT_CLOCK,
            "current_period": current,
            "open_periods": open_periods,
            "sealed_periods": sealed_periods,
            "meter_count": meters,
            "pending_variances": pending,
            "recent_batches": batches,
        }
    finally:
        conn.close()
