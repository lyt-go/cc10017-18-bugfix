"""水位纠正回执：检测事件时间水位倒退，生成可恢复、可交付的纠正回执。

缺陷背景：消费偏移已经前进，但事件时间水位从上一检查版本倒退（颠倒记录），
按当前记录筛选会得到"没有滞后"的错误结果，质量规则因此漏掉真实滞后分区。

写入链路（顺着当前数据流）：
1. 读取订单主题双水位记录（事件时间水位 + 处理时间水位），对比上一检查版本；
2. 生成水位纠正回执：保留纠正前后水位、旧检查版本与受影响窗口；
3. 按"事件时间水位只能按检查版本向前修正"把水位标记写回，最后成功位置随之推进；
4. 跨模块把回执从水位追踪交付给流式质量（保留当前版本），重新检查受影响窗口。

恢复与冲突约定：
- 写入中断后按最后成功位置续写，同一检查版本不会重复写入；
- 同一（数据主题, 分区编号）只保留一份回执；旧检查版本与新消费偏移相遇时
  回退旧水位与按新偏移重算只能二选一，冲突取舍让回执产生新版本而不是重复记录。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.services.stream_quality import StreamQualityService
from app.store import store

MODULE = "watermark_correction"
LIST_FIELDS = ["回执编号", "数据主题", "分区编号", "标记编号", "旧检查版本", "检查版本", "冲突判定", "最后成功位置"]
STATUSES = ["写入中", "已生成", "已交付", "已应用"]
FORWARD_RULE = "事件时间水位只能向前修正：保留较大水位"
DEFAULT_WINDOW_MINUTES = 5


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _to_int(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _version_no(version: Any) -> int:
    return _to_int(str(version or "").strip().lstrip("vV"))


def _next_version(version: Any) -> str:
    return f"v{_version_no(version) + 1}"


def _max_ts_text(first: str, second: str) -> str:
    """事件时间水位只许向前：取两个候选里较大的一个。"""
    ts_first, ts_second = _parse_ts(first), _parse_ts(second)
    if ts_first is None:
        return second
    if ts_second is None:
        return first
    return first if ts_first >= ts_second else second


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


class WatermarkCorrectionService:
    """水位纠正回执的生成、写回、交付、恢复与查询。"""

    # ───────────────────────────────
    # 查询：可按条件检索、排序和翻页
    # ───────────────────────────────

    def list_receipts(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        sort: str | None = None,
        order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE))
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("回执编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = _sort_rows(rows, sort, order)
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, receipt_id)

    def receipts_for_watermark(self, watermark_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(MODULE) if _to_int(row.get("watermark_id")) == int(watermark_id)]

    # ───────────────────────────────
    # 上报读数：正常前进直接接受；倒退或版本冲突进入纠正链路
    # ───────────────────────────────

    def report_reading(
        self, entry: dict[str, Any], payload: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
        prev_snapshot = self._snapshot(entry)
        current_version = str(entry.get("检查版本") or "v1")
        current_event = str(entry.get("事件时间水位") or "")
        new_event = str(payload.get("事件时间水位") or current_event)
        new_proc = str(payload.get("处理时间水位") or entry.get("处理时间水位") or "")
        new_offset = str(payload.get("消费偏移") or entry.get("消费偏移") or "0")
        new_late = str(payload.get("延迟记录数") or entry.get("延迟记录数") or "0")
        base_version = str(payload.get("基准版本") or "").strip()
        provided_version = str(payload.get("检查版本") or "").strip()

        conflict_reasons: list[str] = []
        if base_version and base_version != current_version:
            conflict_reasons.append(f"基准版本 {base_version} 与当前检查版本 {current_version} 不一致")
        if provided_version and _version_no(provided_version) <= _version_no(current_version):
            conflict_reasons.append(f"上报检查版本 {provided_version} 不晚于当前检查版本 {current_version}")
        conflict = bool(conflict_reasons)

        current_ts, new_ts = _parse_ts(current_event), _parse_ts(new_event)
        regression = bool(current_ts and new_ts and new_ts < current_ts)

        if not regression and not conflict:
            entry["_prev_check"] = prev_snapshot
            entry["事件时间水位"] = new_event
            entry["处理时间水位"] = new_proc
            entry["消费偏移"] = str(max(_to_int(new_offset), _to_int(entry.get("消费偏移"))))
            entry["延迟记录数"] = new_late
            entry["检查版本"] = provided_version or _next_version(current_version)
            entry["检查时间"] = datetime.now().isoformat()
            entry["inverted"] = False
            return entry, None, "水位读数已接受，事件时间水位正常前进"

        # 回退到旧水位与按新偏移重算只能二选一：事件时间水位只能向前修正
        corrected_event = _max_ts_text(current_event, new_event)
        candidates = sorted({text for text in (current_event, new_event) if text})
        if corrected_event == current_event and not conflict:
            return entry, None, (
                f"上报事件时间水位 {new_event} 倒退，已被最后成功位置 "
                f"{current_version}@{entry.get('消费偏移')} 覆盖，按向前修正规则跳过重复写入"
            )

        if corrected_event == current_event:
            # 冲突但当前水位已是最前：保留当前版本，只让回执产生新版本
            new_version = current_version
        elif provided_version and _version_no(provided_version) > _version_no(current_version):
            new_version = provided_version
        else:
            new_version = _next_version(current_version)

        receipt, _ = self._upsert_receipt(
            entry=entry,
            old_version=current_version,
            new_version=new_version,
            prev_snapshot=prev_snapshot,
            reported_event=new_event,
            corrected_event=corrected_event,
            new_proc=new_proc,
            new_offset=new_offset,
            reported_late=new_late,
            conflict="；".join(conflict_reasons),
            candidates=candidates,
        )
        self._apply_receipt(receipt)
        self._deliver_receipt(receipt)
        message = (
            f"检测到{'版本冲突' if conflict else '事件时间水位倒退'}，已按检查版本向前修正："
            f"{new_event} → {corrected_event}，回执 {receipt['回执编号']}（版本 v{receipt['版本']}）"
        )
        return entry, receipt, message

    # ───────────────────────────────
    # 纠正存储态倒退：读取双水位记录，生成回执并写回
    # ───────────────────────────────

    def correct_entry(self, entry: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        prev_check = entry.get("_prev_check") or {}
        prev_event = str(prev_check.get("事件时间水位") or "")
        current_event = str(entry.get("事件时间水位") or "")
        current_ts, prev_ts = _parse_ts(current_event), _parse_ts(prev_event)
        regression = bool(current_ts and prev_ts and current_ts < prev_ts)
        if not regression:
            return None, "未发现事件时间水位倒退，无需纠正"

        prev_snapshot = self._snapshot(entry)
        old_version = str(entry.get("检查版本") or "v1")
        new_version = _next_version(old_version)
        corrected_event = _max_ts_text(current_event, prev_event)
        offset_note = ""
        if _to_int(entry.get("消费偏移")) > _to_int(prev_check.get("消费偏移")):
            offset_note = f"消费偏移 {prev_check.get('消费偏移')}→{entry.get('消费偏移')} 已前进，"

        receipt, _ = self._upsert_receipt(
            entry=entry,
            old_version=old_version,
            new_version=new_version,
            prev_snapshot=prev_snapshot,
            reported_event=current_event,
            corrected_event=corrected_event,
            new_proc=str(entry.get("处理时间水位") or ""),
            new_offset=str(entry.get("消费偏移") or "0"),
            reported_late=str(entry.get("延迟记录数") or "0"),
            conflict="",
            candidates=sorted({current_event, prev_event}),
        )
        self._apply_receipt(receipt)
        self._deliver_receipt(receipt)
        return receipt, (
            f"{offset_note}事件时间水位 {prev_check.get('检查版本')} 的 {prev_event} 倒退到 {current_event}，"
            f"已向前修正并生成回执 {receipt['回执编号']}（{old_version}→{new_version}），"
            f"受影响窗口 {len(receipt['受影响窗口'])} 个已交付流式质量重新检查"
        )

    # ───────────────────────────────
    # 中断恢复：按最后成功位置续写，避免重复写入
    # ───────────────────────────────

    def resume(self) -> list[dict[str, Any]]:
        resumed: list[dict[str, Any]] = []
        for receipt in list(store.rows(MODULE)):
            if receipt.get("status") == "已应用":
                continue
            if receipt.get("status") == "写入中":
                self._apply_receipt(receipt)
            if receipt.get("status") == "已生成":
                self._deliver_receipt(receipt)
            elif receipt.get("status") == "已交付":
                StreamQualityService().apply_correction_receipt(receipt)
            resumed.append(receipt)
        return resumed

    # ───────────────────────────────
    # 内部：回执生成（幂等）、写回水位标记、交付流式质量
    # ───────────────────────────────

    def _snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "检查版本": str(entry.get("检查版本") or "v1"),
            "事件时间水位": str(entry.get("事件时间水位") or ""),
            "处理时间水位": str(entry.get("处理时间水位") or ""),
            "消费偏移": str(entry.get("消费偏移") or "0"),
        }

    def _find_receipt(self, topic: str, partition: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("数据主题")) == topic and str(row.get("分区编号")) == partition:
                return row
        return None

    def _upsert_receipt(
        self,
        *,
        entry: dict[str, Any],
        old_version: str,
        new_version: str,
        prev_snapshot: dict[str, Any],
        reported_event: str,
        corrected_event: str,
        new_proc: str,
        new_offset: str,
        reported_late: str,
        conflict: str,
        candidates: list[str],
    ) -> tuple[dict[str, Any], bool]:
        topic = str(entry.get("数据主题") or "")
        partition = str(entry.get("分区编号") or "")
        existing = self._find_receipt(topic, partition)
        if existing is not None and not conflict and _version_no(new_version) <= _version_no(existing.get("检查版本")):
            return existing, False  # 最后成功位置已覆盖该检查版本，避免重复写入

        windows = self._affected_windows(_parse_ts(reported_event), _parse_ts(corrected_event), topic)
        corrected_late = _to_int(reported_late) * (len(windows) + 1)
        now = datetime.now().isoformat()
        if existing is None:
            rows = store.rows(MODULE)
            receipt_id = max((_to_int(row.get("id")) for row in rows), default=0) + 1
            existing = {
                "id": receipt_id,
                "回执编号": f"CORR-{receipt_id:04d}",
                "版本": 0,
                "status": "写入中",
                "pending": True,
                "abnormal": False,
                "创建时间": now,
            }
            rows.append(existing)
        # 同一分区只保留一份回执：冲突取舍让回执产生新版本而不是重复记录
        existing.update({
            "数据主题": topic,
            "分区编号": partition,
            "标记编号": entry.get("标记编号"),
            "watermark_id": entry.get("id"),
            "旧检查版本": old_version,
            "检查版本": new_version,
            "纠正前事件时间水位": reported_event,
            "纠正后事件时间水位": corrected_event,
            "纠正前处理时间水位": prev_snapshot.get("处理时间水位") or entry.get("处理时间水位"),
            "纠正后处理时间水位": new_proc,
            "纠正前消费偏移": prev_snapshot.get("消费偏移") or entry.get("消费偏移"),
            "纠正后消费偏移": str(max(_to_int(new_offset), _to_int(prev_snapshot.get("消费偏移")))),
            "纠正前延迟记录数": str(reported_late),
            "纠正后延迟记录数": str(corrected_late),
            "候选水位": candidates,
            "冲突判定": conflict or "事件时间水位倒退，按检查版本向前修正",
            "受影响窗口": windows,
            "status": "写入中",
            "最后成功位置": f"{old_version}@{prev_snapshot.get('消费偏移') or entry.get('消费偏移')}",
            "更新时间": now,
        })
        existing["版本"] = _to_int(existing.get("版本")) + 1
        return existing, True

    def _apply_receipt(self, receipt: dict[str, Any]) -> bool:
        """把纠正后的水位写回水位标记；只许向前，已覆盖的检查版本直接跳过。"""
        entry = store.find("watermark_track", _to_int(receipt.get("watermark_id")))
        if entry is None:
            return False
        current_version = str(entry.get("检查版本") or "v1")
        target_version = str(receipt.get("检查版本") or current_version)
        current_ts = _parse_ts(entry.get("事件时间水位"))
        target_ts = _parse_ts(receipt.get("纠正后事件时间水位"))
        already_covered = (
            _version_no(current_version) >= _version_no(target_version)
            and current_ts is not None and target_ts is not None and current_ts >= target_ts
        )
        if not already_covered:
            entry["事件时间水位"] = _max_ts_text(
                str(entry.get("事件时间水位") or ""), str(receipt.get("纠正后事件时间水位") or ""))
            entry["处理时间水位"] = receipt.get("纠正后处理时间水位") or entry.get("处理时间水位")
            entry["消费偏移"] = str(max(_to_int(entry.get("消费偏移")), _to_int(receipt.get("纠正后消费偏移"))))
            entry["延迟记录数"] = str(receipt.get("纠正后延迟记录数") or entry.get("延迟记录数"))
            entry["_prev_check"] = {
                "检查版本": receipt.get("旧检查版本"),
                "事件时间水位": receipt.get("纠正前事件时间水位"),
                "处理时间水位": receipt.get("纠正前处理时间水位"),
                "消费偏移": receipt.get("纠正前消费偏移"),
            }
            entry["检查版本"] = target_version
            entry["检查时间"] = datetime.now().isoformat()
            entry["inverted"] = False
            receipt["最后成功位置"] = f"{target_version}@{entry['消费偏移']}"
        receipt["status"] = "已生成"
        receipt["更新时间"] = datetime.now().isoformat()
        return not already_covered

    def _deliver_receipt(self, receipt: dict[str, Any]) -> dict[str, Any] | None:
        """跨模块交付：水位追踪 → 流式质量，交付过程保留当前检查版本。"""
        if receipt.get("status") != "已生成":
            return None
        receipt["status"] = "已交付"
        receipt["交付时间"] = datetime.now().isoformat()
        result = StreamQualityService().apply_correction_receipt(receipt)
        receipt["更新时间"] = datetime.now().isoformat()
        return result

    def _affected_windows(self, start: datetime | None, end: datetime | None, topic: str) -> list[str]:
        if start is None or end is None or start >= end:
            return []
        delta = self._window_delta(topic)
        windows: list[str] = []
        cursor, guard = start, 0
        while cursor < end and guard < 500:
            nxt = min(cursor + delta, end)
            windows.append(f"{cursor.isoformat()}/{nxt.isoformat()}")
            cursor = nxt
            guard += 1
        return windows

    def _window_delta(self, topic: str) -> timedelta:
        rule: dict[str, Any] | None = None
        for row in store.rows("stream_quality"):
            if row.get("监控主题") == topic:
                rule = row
                if row.get("status") == "已生效":
                    break
        text = str((rule or {}).get("检测窗口") or "").strip().lower()
        try:
            if text.endswith("min"):
                return timedelta(minutes=float(text[:-3]))
            if text.endswith("h"):
                return timedelta(hours=float(text[:-1]))
            if text.endswith("s"):
                return timedelta(seconds=float(text[:-1]))
        except ValueError:
            pass
        return timedelta(minutes=DEFAULT_WINDOW_MINUTES)


correction_service = WatermarkCorrectionService()
