"""命运架构重跑对齐：已决命运与批键集合的正确性核对。

四类撕裂夹具逐一回归：
  1. 重跑吞已决   → test_rerun_keeps_decided_fate
  2. 同键两批未决 → test_no_dual_pending_same_key
  3. superseded 仍计数 → test_superseded_batch_never_counted
  4. 抢跑半截     → test_crash_mid_align_rolls_back_everything
                     test_concurrent_align_no_double_open

并在同一组用例里同时按两种架构口径核对（两者必须收敛成同一个答案）：
  - 命运架构：重跑后已决键仍已决，现行未决键集合不被重跑吞掉/重开；
  - 最新批架构：总览未决 == 现行批（唯一 open 批）未决，键集合一致。
"""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

from app.services import batch_rollup, reconcile_service
from tests import helpers
from tests.helpers import (
    AS_OF,
    all_variance_rows,
    batch_statuses,
    count,
    live_variance,
    load_actual,
    load_estimate,
    query_all,
    query_one,
)

P = "P1"


def seed_three_variance_meters():
    # M1/M2/M3 估实不一致，首跑各产生一条 pending 差额
    load_estimate("M1", P, 12.0, "1")
    load_actual("M1", P, 10.0, "1")
    load_estimate("M2", P, 20.0, "2")
    load_actual("M2", P, 18.0, "2")
    load_estimate("M3", P, 30.0, "3")
    load_actual("M3", P, 25.0, "3")


def open_batch_ids(period: str) -> list[str]:
    return [
        r["batch_id"]
        for r in query_all(
            "SELECT batch_id FROM reconcile_batches WHERE period_id=? AND status='open'",
            (period,),
        )
    ]


def test_first_align_creates_single_open_batch_with_three_pending(db):
    seed_three_variance_meters()
    out = reconcile_service.run_align(P, AS_OF)
    assert out["status"] == "open"
    assert out["variance_count"] == 3
    assert out["superseded_batches"] == 0
    assert open_batch_ids(P) == [out["batch_id"]]
    # 数据库层硬不变量：同键不存在两条现行差额
    dupes = query_all(
        "SELECT period_id, meter_id, COUNT(*) AS c FROM variances "
        "WHERE superseded_at IS NULL GROUP BY period_id, meter_id HAVING c > 1"
    )
    assert dupes == []


def test_rerun_keeps_decided_fate(db):
    """夹具①：重跑吞已决——已处置键不得被重新变未决，痕迹保留可追溯。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    v_m1 = live_variance(P, "M1")
    assert v_m1["fate"] == "pending"

    # 处置 M1（认短）、M2（隔离）
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")
    v_m2 = live_variance(P, "M2")
    reconcile_service.set_fate(v_m2["variance_id"], "quarantine")

    # 重跑对齐（同 as_of，闭包未变）
    b2 = reconcile_service.run_align(P, AS_OF)
    assert b2["batch_id"] != b1["batch_id"]
    assert b2["decided_kept"] == ["M1", "M2"]
    assert b2["new_pending_keys"] == ["M3"]

    # 命运架构断言：M1/M2 仍已决，且不存在新的现行差额把它们变回 pending
    assert live_variance(P, "M1") is None
    assert live_variance(P, "M2") is None
    m1_history = all_variance_rows(P, "M1")
    assert len(m1_history) == 1
    assert m1_history[0]["fate"] == "accept_short"
    assert m1_history[0]["superseded_at"] is not None
    m2_history = all_variance_rows(P, "M2")
    assert m2_history[0]["fate"] == "quarantine"
    assert m2_history[0]["superseded_at"] is not None

    # M3 未决随新批继续，仍是现行 pending
    m3 = live_variance(P, "M3")
    assert m3 is not None and m3["fate"] == "pending" and m3["batch_id"] == b2["batch_id"]

    # 总览未决只认现行 pending：只剩 M3 一条，M1/M2 绝不被重跑重开
    assert batch_rollup.live_pending_count(db.connect(), P) == 1


def test_no_dual_pending_same_key(db):
    """夹具②：同键两批未决——重跑后旧批 pending 必须全部收敛为痕迹。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    b2 = reconcile_service.run_align(P, AS_OF)
    b3 = reconcile_service.run_align(P, AS_OF)

    # 每键全历史有 3 行，但现行行恰好 1 行
    for mid in ("M1", "M2", "M3"):
        rows = all_variance_rows(P, mid)
        assert len(rows) == 3
        live = [r for r in rows if r["superseded_at"] is None]
        assert len(live) == 1
        assert live[0]["batch_id"] == b3["batch_id"]
        assert live[0]["fate"] == "pending"

    # 旧批全部 superseded，只有最新批 open；不存在双现行 open
    statuses = batch_statuses(P)
    assert statuses[b1["batch_id"]] == "superseded"
    assert statuses[b2["batch_id"]] == "superseded"
    assert statuses[b3["batch_id"]] == "open"
    assert open_batch_ids(P) == [b3["batch_id"]]

    # 部分唯一索引真的在库层：强行插同键现行行必须失败
    from app.db import connect
    conn = connect()
    try:
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO variances(variance_id,batch_id,meter_id,period_id,"
                "delta,fate,superseded_at) VALUES ('VX',?,'M1','P1',1.0,'pending',NULL)",
                (b3["batch_id"],),
            )
    finally:
        conn.close()


def test_superseded_batch_never_counted(db):
    """夹具③：superseded 仍计数——总览/rollup/最新批三口径必须同一个数。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    # 处置 M1 后重跑：b1 含 3 条 pending，b2 含 2 条 pending
    v_m1 = live_variance(P, "M1")
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")
    b2 = reconcile_service.run_align(P, AS_OF)

    from app.db import connect
    conn = connect()
    try:
        # 最新批架构口径：现行批 pending
        cur = batch_rollup.current_batch(conn, P)
        assert cur["batch_id"] == b2["batch_id"]
        current_batch_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM variances "
            "WHERE batch_id=? AND fate='pending' AND superseded_at IS NULL",
            (b2["batch_id"],),
        ).fetchone()["c"]
        # 命运架构口径：现行 pending 键集合
        live_pending = batch_rollup.live_pending_count(conn, P)
        report = batch_rollup.rollup_report(conn, P)
    finally:
        conn.close()

    # 两种架构口径在此收敛：都是 2（M2/M3），不是 3+2=5
    assert current_batch_pending == live_pending == report["live_pending"] == 2
    assert report["rolled_pending"] == 2
    assert report["current_batch_id"] == b2["batch_id"]

    # 旧批 b1 在报告里只能以痕迹身份出现：live_pending=0，trace 行不进总览
    b1_entry = next(x for x in report["batches"] if x["batch_id"] == b1["batch_id"])
    assert b1_entry["status"] == "superseded"
    assert b1_entry["live_pending"] == 0
    assert b1_entry["trace_pending"] == 2  # M2/M3 当时确为 pending，留痕可审计

    # 全周期总览（不带 period）同样不读 superseded 批
    from app.db import connect as c2
    conn2 = c2()
    try:
        assert batch_rollup.live_pending_count(conn2) == 2
    finally:
        conn2.close()


def test_snapshot_key_set_matches_closure_and_union(db):
    """快照键集合与正式差额并集一致：新批 success 时键集合不许撒谎。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    v_m1 = live_variance(P, "M1")
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")
    b2 = reconcile_service.run_align(P, AS_OF)

    snap_keys = {
        r["meter_id"]
        for r in query_all(
            "SELECT meter_id FROM reconcile_snapshots WHERE batch_id=?", (b2["batch_id"],)
        )
    }
    # 快照 = 闭包全集（三只表都在）
    assert snap_keys == {"M1", "M2", "M3"}

    live_keys = {
        r["meter_id"]
        for r in query_all(
            "SELECT meter_id FROM variances WHERE period_id=? AND superseded_at IS NULL",
            (P,),
        )
    }
    decided_keys = {
        r["meter_id"]
        for r in query_all(
            "SELECT DISTINCT meter_id FROM variances "
            "WHERE period_id=? AND superseded_at IS NOT NULL AND fate!='pending'",
            (P,),
        )
    }
    # 正式差额并集（现行未决 ∪ 已决留痕）必须与快照键集合相等
    assert live_keys | decided_keys == snap_keys
    assert live_keys == {"M2", "M3"}
    assert decided_keys == {"M1"}


def test_crash_mid_align_rolls_back_everything(db, monkeypatch):
    """夹具④：抢跑半截——提交前崩溃必须整笔回滚，不留双 open/半截行，HTTP 非 200。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    before_batches = query_all("SELECT batch_id, status FROM reconcile_batches")
    before_vars = query_all("SELECT variance_id, superseded_at FROM variances")

    def boom():
        raise RuntimeError("simulated crash after new-batch writes, before commit")

    monkeypatch.setattr(reconcile_service, "_align_crash_point", boom)
    with pytest.raises(RuntimeError):
        reconcile_service.run_align(P, AS_OF)

    # 库状态与崩溃前逐字节一致：无新批、无新快照、无新差额、旧痕迹未被打
    assert query_all("SELECT batch_id, status FROM reconcile_batches") == before_batches
    assert query_all("SELECT variance_id, superseded_at FROM variances") == before_vars
    assert open_batch_ids(P) == [b1["batch_id"]]
    assert count("reconcile_snapshots") == 3  # 仍只有 b1 的三行快照
    for mid in ("M1", "M2", "M3"):
        v = live_variance(P, mid)
        assert v is not None and v["fate"] == "pending" and v["batch_id"] == b1["batch_id"]


def test_concurrent_align_no_double_open(db):
    """夹具④并发面：双路同时重跑，串行化后仍只有一个现行 open 批、每键一条现行。"""
    seed_three_variance_meters()
    reconcile_service.run_align(P, AS_OF)

    errors: list[Exception] = []
    results: list[dict] = []

    def worker():
        try:
            results.append(reconcile_service.run_align(P, AS_OF))
        except Exception as exc:  # 允许一路因锁/约束失败，但不许留下脏状态
            errors.append(exc)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)

    opens = open_batch_ids(P)
    assert len(opens) == 1, f"出现双现行 open 批: {opens}"
    dupes = query_all(
        "SELECT period_id, meter_id, COUNT(*) AS c FROM variances "
        "WHERE superseded_at IS NULL GROUP BY period_id, meter_id HAVING c > 1"
    )
    assert dupes == []
    # 两路合计成功的批对应唯一现行批；失败路若有，必须是干净拒绝而非 200 式半截
    success_batches = {r["batch_id"] for r in results}
    assert opens[0] in success_batches
    for mid in ("M1", "M2", "M3"):
        rows = all_variance_rows(P, mid)
        live = [r for r in rows if r["superseded_at"] is None]
        assert len(live) == 1


def test_set_fate_rejects_superseded_trace_and_sealed(db):
    """痕迹行禁止翻案；封印期禁止改命运。"""
    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    v_m1 = live_variance(P, "M1")
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")
    reconcile_service.run_align(P, AS_OF)  # 重跑后 v_m1 成为痕迹行

    with pytest.raises(HTTPException) as ei:
        reconcile_service.set_fate(v_m1["variance_id"], "quarantine")
    assert ei.value.status_code == 409

    # 封印期
    cur = query_one(
        "SELECT batch_id FROM reconcile_batches WHERE period_id=? AND status='open'", (P,)
    )
    reconcile_service.seal_period(P, cur["batch_id"], AS_OF)
    v_m3 = live_variance(P, "M3")
    with pytest.raises(HTTPException) as ei2:
        reconcile_service.set_fate(v_m3["variance_id"], "accept_over")
    assert ei2.value.status_code == 409


def test_closure_narrowing_keeps_decided_trace_auditable(db):
    """闭包缩窄（更早 as_of）：掉出新快照的已决键仍留痕，未决计数不重开。"""
    # M1 的行在较晚时刻才装载；先用晚 as_of 对齐并处置
    load_estimate("M1", P, 12.0, "1", loaded_at="2026-10-20T08:00:00+00:00")
    load_actual("M1", P, 10.0, "1", loaded_at="2026-10-20T08:00:00+00:00")
    load_estimate("M2", P, 20.0, "2")
    load_actual("M2", P, 18.0, "2")

    late = "2026-10-31T00:00:00+00:00"
    early = "2026-10-10T00:00:00+00:00"
    b1 = reconcile_service.run_align(P, late)
    v_m1 = live_variance(P, "M1")
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")

    # 用更早 as_of 重跑：M1 掉出闭包，M2 仍在
    b2 = reconcile_service.run_align(P, early)
    snap_keys = {
        r["meter_id"]
        for r in query_all(
            "SELECT meter_id FROM reconcile_snapshots WHERE batch_id=?", (b2["batch_id"],)
        )
    }
    assert snap_keys == {"M2"}

    # M1 无现行差额，但其处置痕迹完整保留
    assert live_variance(P, "M1") is None
    m1_history = all_variance_rows(P, "M1")
    assert len(m1_history) == 1
    assert m1_history[0]["fate"] == "accept_short"
    assert m1_history[0]["superseded_at"] is not None
    assert m1_history[0]["batch_id"] == b1["batch_id"]

    # 现行未决只剩 M2，M1 既不重开也不计数
    assert batch_rollup.live_pending_count(db.connect(), P) == 1
    assert live_variance(P, "M2")["batch_id"] == b2["batch_id"]


def test_overview_endpoint_reports_live_pending_only(db):
    """HTTP 总览口径：pending_variances == 现行批未决，superseded 不参与。"""
    from fastapi.testclient import TestClient
    from app.main import app

    seed_three_variance_meters()
    b1 = reconcile_service.run_align(P, AS_OF)
    v_m1 = live_variance(P, "M1")
    reconcile_service.set_fate(v_m1["variance_id"], "accept_short")
    reconcile_service.run_align(P, AS_OF)

    with TestClient(app) as client:
        ov = client.get("/api/overview").json()
        report = client.get(f"/api/rollup?period_id={P}").json()
        batches = client.get(f"/api/batches?period_id={P}").json()

    assert ov["pending_variances"] == 2
    assert report["live_pending"] == 2
    assert report["rolled_pending"] == 2
    statuses = {b["batch_id"]: b["status"] for b in batches}
    assert sum(1 for s in statuses.values() if s == "open") == 1
    assert statuses[b1["batch_id"]] == "superseded"
