"""全局唯一时钟口径。

架构决策（全局只贯彻这一种）：以**装载钟 loaded_at** 截止为准。
- 对齐 as_of 只纳入 loaded_at <= as_of 的正式估抄/实抄；
- occurred_at 仅作审计展示，绝不参与闭包截止；
- 快照 clock 字段恒为 'loaded_at'；
- 已封印批次的钟与 as_of 在批次行固化，事后不可改。

任何需要"现在时刻"或解析 as_of 的代码都必须走这里，禁止各处自行 datetime.now。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

CLOCK = "loaded_at"
# 发生钟只允许出现在审计/展示语境，不得作为截止钟
AUDIT_CLOCK = "occurred_at"


def now() -> datetime:
    """服务端唯一授时：UTC、秒级精度。loaded_at 与封印时刻均取自此。"""
    return datetime.now(timezone.utc).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


def parse_as_of(raw: str | None) -> datetime:
    """把外部传入的 as_of 解析为带时区的 UTC datetime。

    伪造 as_of 一律拒绝：
    - 空值/非字符串/无法解析 → 400；
    - 未来时刻（晚于服务端当前装载钟）→ 400，as-of 只能回看历史；
    - 朴素时间戳按 UTC 处理（库内 loaded_at 均为 UTC ISO 字符串）。
    """
    if not raw or not isinstance(raw, str):
        raise HTTPException(400, "as_of 必填且为 ISO-8601 时刻")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(400, f"as_of 不是合法 ISO-8601 时刻: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    current = now()
    if dt > current:
        raise HTTPException(
            400,
            f"as_of {dt.isoformat()} 晚于当前装载钟 {current.isoformat()}，禁止预签未来闭包",
        )
    return dt


def assert_clock_supported(clock: str | None) -> None:
    """钟字段只接受 loaded_at；请求 occurred_at 截止即两套真，直接拒。"""
    if clock is not None and clock != CLOCK:
        raise HTTPException(
            400,
            f"本系统全局按装载钟 {CLOCK} 截止，{clock} 仅审计用，不得作为对齐/封印钟",
        )
