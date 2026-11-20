"""水位追踪业务规则：状态流转、字段校验与筛选口径都收在这里。

增强：检查版本跟踪、颠倒记录（消费偏移前进但事件时间水位倒退）识别，
以及水位纠正回执的触发入口（上报读数 / 触发纠正）。
"""
from __future__ import annotations

from typing import Any

from app.services.watermark_correction import correction_service
from app.store import store

MODULE = "watermark_track"
LIST_FIELDS = ["标记编号", "数据主题", "分区编号", "事件时间水位", "处理时间水位", "延迟记录数", "消费偏移", "检查时间", "检查版本"]
REQUIRED_FIELDS = ["标记编号", "数据主题", "分区编号"]
STATUS_ORDER = ["草稿", "正常", "滞后", "已停用"]
ACTION_RULES = {"标记正常": "正常", "标记滞后": "滞后", "停用追踪": "已停用"}
NEGATIVE_ACTIONS = ["停用追踪"]


def _sort_rows(rows: list[dict[str, Any]], sort: str | None, order: str) -> list[dict[str, Any]]:
    if not sort:
        return rows

    def key(row: dict[str, Any]) -> tuple[int, float, str]:
        value = row.get(sort)
        text = "" if value is None else str(value)
        try:
            return (0, float(text), text)
        except ValueError:
            return (1, 0.0, text)

    return sorted(rows, key=key, reverse=str(order).lower() == "desc")


class WatermarkTrackService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        inverted: bool | None = None,
        sort: str | None = None,
        order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("标记编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if inverted is not None:
            rows = [row for row in rows if bool(row.get("inverted")) == inverted]
        rows = _sort_rows(rows, sort, order)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["检查版本"] = str(values.get("检查版本") or "").strip() or "v1"
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["inverted"] = False
        entry["_prev_check"] = None
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"水位标记 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于水位追踪可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"水位标记已{action}"

    # ───────────────────────────────
    # 水位纠正回执：上报读数、触发纠正、回执查询
    # ───────────────────────────────

    def report_reading(
        self, entry_id: int, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None, f"水位标记 {entry_id} 不存在或已归档"
        return correction_service.report_reading(entry, payload)

    def correct_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, None, f"水位标记 {entry_id} 不存在或已归档"
        receipt, message = correction_service.correct_entry(entry)
        return entry, receipt, message

    def list_corrections(self, entry_id: int) -> list[dict[str, Any]] | None:
        if store.find(MODULE, entry_id) is None:
            return None
        return correction_service.receipts_for_watermark(entry_id)
