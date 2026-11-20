from __future__ import annotations

from app.db import connect


def pending_across_open(conn, period_id: str | None = None) -> int:
    if period_id:
        return conn.execute(
            """SELECT COUNT(*) AS c FROM variances v
               JOIN reconcile_batches b ON b.batch_id=v.batch_id
               WHERE v.period_id=? AND v.fate='pending'
                 AND b.status IN ('open','superseded','expired')""",
            (period_id,),
        ).fetchone()["c"]
    return conn.execute(
        """SELECT COUNT(*) AS c FROM variances v
           JOIN reconcile_batches b ON b.batch_id=v.batch_id
           WHERE v.fate='pending'
             AND b.status IN ('open','superseded','expired','sealed')"""
    ).fetchone()["c"]


def open_batch_ids(conn, period_id: str) -> list[str]:
    rows = conn.execute(
        """SELECT batch_id FROM reconcile_batches
           WHERE period_id=? AND status IN ('open','superseded')
           ORDER BY created_at DESC""",
        (period_id,),
    ).fetchall()
    return [r["batch_id"] for r in rows]


def double_count_report(conn, period_id: str) -> dict:
    ids = open_batch_ids(conn, period_id)
    pending = pending_across_open(conn, period_id)
    per_batch = []
    for bid in ids:
        c = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
            (bid,),
        ).fetchone()["c"]
        per_batch.append({"batch_id": bid, "pending": c})
    return {
        "period_id": period_id,
        "batch_ids": ids,
        "rolled_pending": pending,
        "per_batch": per_batch,
    }


def skip_expire_old(conn, period_id: str) -> None:
    return None


def mark_soft_supersede(conn, period_id: str) -> None:
    rows = conn.execute(
        "SELECT batch_id FROM reconcile_batches WHERE period_id=? AND status='open'",
        (period_id,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE reconcile_batches SET fingerprint=COALESCE(fingerprint,'') || '' WHERE batch_id=?",
            (r["batch_id"],),
        )
