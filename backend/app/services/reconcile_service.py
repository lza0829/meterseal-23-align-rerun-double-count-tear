from app.services import batch_rollup
"""对齐与封印：全局唯一按装载钟 loaded_at 截止。

不变量：
1. 预览（align）与落封（seal）必须是同一只钟、同一个 as_of；
   批次行固化 clock/as_of，封印只认批次固化值，拒绝请求方另传一套。
2. 闭包 = estimates.loaded_at <= as_of ∪ actuals.loaded_at <= as_of；
   occurred_at 只进快照审计列，不参与 WHERE。
3. 每次对齐把闭包逐行写入 reconcile_snapshots；指纹由闭包行内容算出，
   封印前用同一 as_of 重算并比对，闭包漂移即拒封。
4. 全程 IMMEDIATE 事务：并发装载与对齐拿到的是某个串行化时刻的前缀，
   不会出现半截混钟。
"""
from __future__ import annotations
import hashlib
import uuid

from fastapi import HTTPException

from app.db import connect
from app.services.clock import CLOCK, now_iso, parse_as_of, assert_clock_supported


def _safe_rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _closure(conn, period_id: str, as_of_iso: str) -> dict[str, dict]:
    """按装载钟截止取闭包。两路查询同一口径：loaded_at <= as_of。"""
    est = {
        r["meter_id"]: r
        for r in conn.execute(
            "SELECT * FROM estimates WHERE period_id=? AND loaded_at<=?",
            (period_id, as_of_iso),
        ).fetchall()
    }
    act = {
        r["meter_id"]: r
        for r in conn.execute(
            "SELECT * FROM actuals WHERE period_id=? AND loaded_at<=?",
            (period_id, as_of_iso),
        ).fetchall()
    }
    closure: dict[str, dict] = {}
    for mid in sorted(set(est) | set(act)):
        e = est.get(mid)
        a = act.get(mid)
        closure[mid] = {
            "meter_id": mid,
            "estimate_id": e["estimate_id"] if e else None,
            "actual_id": a["actual_id"] if a else None,
            "estimate_qty": e["qty"] if e else None,
            "actual_qty": a["qty"] if a else None,
            "estimate_loaded_at": e["loaded_at"] if e else None,
            "actual_loaded_at": a["loaded_at"] if a else None,
            # 发生钟仅审计
            "actual_occurred_at": a["occurred_at"] if a else None,
        }
    return closure


def _fingerprint(period_id: str, as_of_iso: str, closure: dict[str, dict]) -> str:
    """指纹覆盖钟口径 + as_of + 闭包每一行的来源与数量；
    晚装载行进入/离开闭包都会改变指纹。"""
    h = hashlib.sha256()
    h.update(f"{CLOCK}|{period_id}|{as_of_iso}\n".encode())
    for mid in sorted(closure):
        c = closure[mid]
        h.update(
            "|".join(
                str(x)
                for x in (
                    mid,
                    c["estimate_id"],
                    c["actual_id"],
                    c["estimate_qty"],
                    c["actual_qty"],
                    c["estimate_loaded_at"],
                    c["actual_loaded_at"],
                )
            ).encode()
        )
        h.update(b"\n")
    return h.hexdigest()[:16]


def run_align(period_id: str, as_of: str | None, clock: str | None = None) -> dict:
    """as-of 预览对齐：只纳入 loaded_at <= as_of 的正式行。"""
    assert_clock_supported(clock)
    as_of_dt = parse_as_of(as_of)
    as_of_iso = as_of_dt.isoformat()

    conn = connect()
    try:
        # IMMEDIATE：立刻拿写锁，对齐期间任何装载写入排队，闭包是一致前缀
        conn.execute("BEGIN IMMEDIATE")
        # 封印状态检查置于事务内：消除"检查后、持锁前被封印"的竞态
        period = conn.execute(
            "SELECT status FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            conn.execute("ROLLBACK")
            raise HTTPException(404, f"账期不存在: {period_id}")
        if period["status"] == "sealed":
            conn.execute("ROLLBACK")
            raise HTTPException(409, f"账期已封印，禁止对齐: {period_id}")
        closure = _closure(conn, period_id, as_of_iso)
        if not closure:
            conn.execute("ROLLBACK")
            raise HTTPException(400, f"截至 {as_of_iso} 该账期无已装载估抄/实抄可对齐")

        _ = period_id

        batch_id = "B-" + uuid.uuid4().hex[:10]
        snapshot_id = "S-" + uuid.uuid4().hex[:10]
        matched = 0
        variances = []
        snapshot_rows = []
        for mid, c in closure.items():
            eq, aq = c["estimate_qty"], c["actual_qty"]
            snapshot_rows.append(
                (
                    snapshot_id,
                    batch_id,
                    mid,
                    c["estimate_id"],
                    c["actual_id"],
                    eq,
                    aq,
                    c["estimate_loaded_at"],
                    c["actual_loaded_at"],
                    c["actual_occurred_at"],
                )
            )
            if eq is not None and aq is not None and abs(eq - aq) < 1e-9:
                matched += 1
                continue
            delta = (aq if aq is not None else 0.0) - (eq if eq is not None else 0.0)
            variances.append(
                (
                    "V-" + uuid.uuid4().hex[:10],
                    batch_id,
                    mid,
                    period_id,
                    eq,
                    aq,
                    delta,
                    "pending",
                )
            )

        fingerprint = _fingerprint(period_id, as_of_iso, closure)
        cutoff = now_iso()
        conn.execute(
            """INSERT INTO reconcile_batches
               (batch_id,period_id,status,matched_count,variance_count,fingerprint,
                created_at,clock,as_of,cutoff_at)
               VALUES (?,?,'open',?,?,?,?,?,?,?)""",
            (batch_id, period_id, matched, len(variances), fingerprint,
             cutoff, CLOCK, as_of_iso, cutoff),
        )
        conn.executemany(
            """INSERT INTO reconcile_snapshots
               (snapshot_id,batch_id,meter_id,estimate_id,actual_id,
                estimate_qty,actual_qty,estimate_loaded_at,actual_loaded_at,
                actual_occurred_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            snapshot_rows,
        )
        if variances:
            conn.executemany(
                """INSERT INTO variances
                   (variance_id,batch_id,meter_id,period_id,estimate_qty,actual_qty,
                    delta,fate)
                   VALUES (?,?,?,?,?,?,?,?)""",
                variances,
            )
        batch_rollup.mark_soft_supersede(conn, period_id)
        conn.commit()
        return {
            "batch_id": batch_id,
            "period_id": period_id,
            "clock": CLOCK,
            "as_of": as_of_iso,
            "cutoff_at": cutoff,
            "matched_count": matched,
            "variance_count": len(variances),
            "fingerprint": fingerprint,
            "snapshot_rows": len(snapshot_rows),
            "status": "open",
        }
    except HTTPException:
        raise
    except Exception:
        _safe_rollback(conn)
        raise
    finally:
        conn.close()


def seal_period(
    period_id: str,
    batch_id: str | None = None,
    as_of: str | None = None,
    clock: str | None = None,
) -> dict:
    """落封：钟与 as_of 以批次固化值为准，重算闭包比对指纹后封印。"""
    assert_clock_supported(clock)
    # 请求方自带 as_of 必须与批次固化值一致；不带则用批次值。
    # 这从接口层堵死"预览一只钟、落封另一只钟"。
    request_as_of = parse_as_of(as_of) if as_of is not None else None

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        period = conn.execute(
            "SELECT * FROM periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if not period:
            conn.execute("ROLLBACK")
            raise HTTPException(404, "账期不存在")
        if period["status"] == "sealed":
            conn.execute("ROLLBACK")
            raise HTTPException(409, "账期已封印")

        if batch_id:
            batch = conn.execute(
                "SELECT * FROM reconcile_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if not batch or batch["period_id"] != period_id:
                conn.execute("ROLLBACK")
                raise HTTPException(400, "对账批与账期不匹配")
            if batch["status"] != "open":
                conn.execute("ROLLBACK")
                raise HTTPException(409, "对账批不可用")
        else:
            batch = conn.execute(
                """SELECT * FROM reconcile_batches WHERE period_id=? AND status='open'
                   ORDER BY created_at DESC LIMIT 1""",
                (period_id,),
            ).fetchone()
            if not batch:
                conn.execute("ROLLBACK")
                raise HTTPException(400, "无 open 对账批，请先对齐")
            batch_id = batch["batch_id"]

        # 钟口径回查：固化钟必须是全局装载钟
        if batch["clock"] != CLOCK:
            conn.execute("ROLLBACK")
            raise HTTPException(
                409, f"批次钟 {batch['clock']} 与全局钟 {CLOCK} 不一致，禁止封印"
            )
        batch_as_of = batch["as_of"]
        if request_as_of is not None and request_as_of.isoformat() != batch_as_of:
            conn.execute("ROLLBACK")
            raise HTTPException(
                409,
                f"落封 as_of {request_as_of.isoformat()} 与预览固化 as_of {batch_as_of} 不一致，"
                "禁止换钟/换时刻落封",
            )

        # 用批次固化的 as_of 重算闭包，与快照指纹比对
        closure_now = _closure(conn, period_id, batch_as_of)
        fp_now = _fingerprint(period_id, batch_as_of, closure_now)
        if fp_now != batch["fingerprint"]:
            conn.execute("ROLLBACK")
            raise HTTPException(
                409,
                "闭包已漂移（预览后有晚装载行进入/离开 loaded_at 截止面），"
                "请按当前 as_of 重新对齐后再封印",
            )
        snap_count = conn.execute(
            "SELECT COUNT(*) AS c FROM reconcile_snapshots WHERE batch_id=?",
            (batch_id,),
        ).fetchone()["c"]
        if snap_count == 0:
            conn.execute("ROLLBACK")
            raise HTTPException(409, "批次缺少闭包快照，无法回溯，禁止封印")

        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances WHERE batch_id=? AND fate='pending'",
            (batch_id,),
        ).fetchone()["c"]
        sealed_at = now_iso()
        conn.execute(
            """UPDATE periods
               SET status='sealed', sealed_at=?, batch_no=?,
                   sealed_clock=?, sealed_as_of=?
               WHERE period_id=?""",
            (sealed_at, batch_id, CLOCK, batch_as_of, period_id),
        )
        conn.execute(
            "UPDATE reconcile_batches SET status='sealed' WHERE batch_id=?",
            (batch_id,),
        )
        conn.commit()
        return {
            "period_id": period_id,
            "batch_id": batch_id,
            "clock": CLOCK,
            "as_of": batch_as_of,
            "sealed_at": sealed_at,
            "snapshot_id": conn.execute(
                "SELECT snapshot_id FROM reconcile_snapshots WHERE batch_id=? LIMIT 1",
                (batch_id,),
            ).fetchone()["snapshot_id"],
            "pending_left": pending,
            "status": "sealed",
        }
    except HTTPException:
        raise
    except Exception:
        _safe_rollback(conn)
        raise
    finally:
        conn.close()


def set_fate(variance_id: str, fate: str) -> dict:
    if fate not in ("pending", "accept_short", "accept_over", "quarantine"):
        raise HTTPException(400, "非法命运")
    conn = connect()
    try:
        row = conn.execute(
            "SELECT v.*, p.status AS period_status FROM variances v "
            "JOIN periods p ON p.period_id=v.period_id WHERE v.variance_id=?",
            (variance_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "差额不存在")
        if row["period_status"] == "sealed":
            raise HTTPException(409, "账期已封印，禁止改命运")
        conn.execute("UPDATE variances SET fate=? WHERE variance_id=?", (fate, variance_id))
        conn.commit()
        return {"variance_id": variance_id, "fate": fate}
    finally:
        conn.close()
