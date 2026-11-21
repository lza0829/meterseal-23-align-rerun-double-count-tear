"""批次汇总与新旧批交接：全局贯彻**命运架构**。

架构决策（全局只贯彻这一种）：以已决命运与痕迹为准。
- 重跑对齐不得把已处置键重新变回未决；新批只能收"仍未决的现行键"。
- 一个账期至多一个现行对账批（status='open'）。重跑先把旧 open 批置为
  'superseded'，其名下差额全部打 superseded_at 留痕，再写新批。
- 现行差额 = variances.superseded_at IS NULL。同键禁止两条现行差额，
  由部分唯一索引 ux_variances_live 在数据库层硬保证，应用层兜不住时直接报错。
- 未决只数 fate='pending' 的现行差额。superseded 批的行必然 superseded_at
  非空，"旧批标 superseded 仍驱动总览未决"在 SQL 口径上不可能发生。
- 最新批视图与命运痕迹视图给出同一个答案：现行批的 pending 数恒等于
  现行未决键集合大小，禁止跨批加总。
"""
from __future__ import annotations

from app.db import connect


def live_pending_count(conn, period_id: str | None = None) -> int:
    """现行未决差额数：只认真痕迹（superseded_at IS NULL）+ pending 命运。

    不 JOIN 批次状态：痕迹列本身就是权威，批状态写错也不会让旧批未决复活。
    """
    sql = (
        "SELECT COUNT(*) AS c FROM variances "
        "WHERE superseded_at IS NULL AND fate='pending'"
    )
    params: tuple = ()
    if period_id:
        sql += " AND period_id=?"
        params = (period_id,)
    return conn.execute(sql, params).fetchone()["c"]


def current_batch(conn, period_id: str):
    """账期唯一现行批；不存在返回 None。"""
    return conn.execute(
        """SELECT * FROM reconcile_batches
           WHERE period_id=? AND status='open'
           ORDER BY created_at DESC, batch_id DESC LIMIT 1""",
        (period_id,),
    ).fetchone()


def supersede_live(conn, period_id: str, stamp: str) -> dict:
    """旧现行批整体交接（必须在 IMMEDIATE 事务内调用）。

    顺序：先把旧 open 批名下所有现行差额打痕迹，再把批置 superseded。
    正常不变量下至多一个旧 open 批；循环写法同时兜底历史撕裂库。
    返回交接计数，供调用方写进响应/审计。
    """
    old_rows = conn.execute(
        "SELECT batch_id FROM reconcile_batches "
        "WHERE period_id=? AND status='open' ORDER BY created_at DESC",
        (period_id,),
    ).fetchall()
    stamped = 0
    for r in old_rows:
        cur = conn.execute(
            "UPDATE variances SET superseded_at=? "
            "WHERE batch_id=? AND superseded_at IS NULL",
            (stamp, r["batch_id"]),
        )
        stamped += cur.rowcount
        conn.execute(
            "UPDATE reconcile_batches SET status='superseded' WHERE batch_id=?",
            (r["batch_id"],),
        )
    return {"superseded_batches": len(old_rows), "stamped_variances": stamped}


def rollup_report(conn, period_id: str) -> dict:
    """账期未决总览：现行未决一个口径，历史批次只作痕迹展示。

    live_pending 与 rolled_pending 同值——保留旧字段名但语义已改为
    "现行键集合大小"，任何调用方都拿不到多批加总的数字。
    """
    batches = conn.execute(
        "SELECT batch_id, status FROM reconcile_batches "
        "WHERE period_id=? ORDER BY created_at DESC, batch_id DESC",
        (period_id,),
    ).fetchall()
    per_batch = []
    for b in batches:
        live = conn.execute(
            "SELECT COUNT(*) AS c FROM variances "
            "WHERE batch_id=? AND superseded_at IS NULL AND fate='pending'",
            (b["batch_id"],),
        ).fetchone()["c"]
        trace = conn.execute(
            "SELECT COUNT(*) AS c FROM variances "
            "WHERE batch_id=? AND fate='pending'",
            (b["batch_id"],),
        ).fetchone()["c"]
        per_batch.append(
            {
                "batch_id": b["batch_id"],
                "status": b["status"],
                "live_pending": live,
                # 痕迹口径：含已被取代行，仅供审计，绝不参与总览计数
                "trace_pending": trace,
            }
        )
    cur = current_batch(conn, period_id)
    live = live_pending_count(conn, period_id)
    return {
        "period_id": period_id,
        "current_batch_id": cur["batch_id"] if cur else None,
        "live_pending": live,
        "rolled_pending": live,
        "batches": per_batch,
    }
