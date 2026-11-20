"""流式质量业务规则：质量规则管理、校验执行与死信联动。

增强：质量规则生效时触发水位检查，异常时生成死信记录；
接入水位纠正回执，重新检查受影响窗口，真实滞后分区不再漏报。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "stream_quality"
LIST_FIELDS = ["规则编号", "监控主题", "校验类型", "告警阈值", "采样比例", "检测窗口", "阻断动作", "规则版本"]
REQUIRED_FIELDS = ["规则编号", "监控主题", "校验类型"]
STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]


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


class StreamQualityService:
    def list_entries(
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
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
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
        # 校验监控主题
        topic_code = values.get("监控主题")
        if topic_code:
            topic = self._find_topic(topic_code)
            if not topic:
                return None, [f"监控主题 {topic_code} 未注册"]

        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"质量规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于流式质量可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "标记生效":
            topic_code = entry.get("监控主题")
            watermark = self._find_watermark(topic_code)
            if not watermark:
                return None, f"主题 {topic_code} 未配置水位追踪"
            # 执行质量检查
            self._run_quality_check(entry, watermark)

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"质量规则已{action}"

    # ───────────────────────────────
    # 水位纠正回执：消费回执并重新检查受影响窗口
    # ───────────────────────────────

    def apply_correction_receipt(self, receipt: dict[str, Any]) -> dict[str, Any] | None:
        """消费水位纠正回执：按纠正后水位重新检查受影响窗口。

        幂等：同一回执同一检查版本只写一次检查结果与死信，避免重复写入。
        """
        topic = receipt.get("数据主题")
        rule = self._find_active_rule(topic)
        if rule is None:
            return None
        self._ensure_artifact_table("quality_check_results")
        for row in store.rows("quality_check_results"):
            if row.get("receipt_id") == receipt.get("id") and row.get("检查版本") == receipt.get("检查版本"):
                # 同一检查版本已重检过：不重复写入，但回执应收尾为已应用
                receipt["status"] = "已应用"
                receipt["更新时间"] = datetime.now().isoformat()
                return row

        try:
            threshold = float(rule.get("告警阈值", "0"))
        except (ValueError, TypeError):
            threshold = 0.0
        try:
            late_total = float(receipt.get("纠正后延迟记录数", "0"))
        except (ValueError, TypeError):
            late_total = 0.0
        windows = list(receipt.get("受影响窗口") or [])
        window_late = round(late_total / len(windows), 2) if windows else late_total

        violations: list[dict[str, Any]] = []
        if late_total > threshold:
            violations.append({
                "type": "延迟超限",
                "detail": f"纠正后延迟记录 {int(late_total)} 超过阈值 {threshold}，涉及 {len(windows)} 个受影响窗口",
                "topic": topic,
                "windows": windows,
            })
            self._create_dead_letter_for_receipt(rule, receipt, late_total)

        result = {
            "rule_id": rule.get("id"),
            "rule_code": rule.get("规则编号"),
            "topic": topic,
            "threshold": threshold,
            "late_count": late_total,
            "window_late": window_late,
            "受影响窗口": windows,
            "检查版本": receipt.get("检查版本"),
            "receipt_id": receipt.get("id"),
            "回执编号": receipt.get("回执编号"),
            "recheck": True,
            "violations": violations,
            "checked_at": datetime.now().isoformat(),
            "passed": len(violations) == 0,
        }
        store.rows("quality_check_results").append(result)

        if violations:
            # 真实滞后分区不再漏报：水位标记回到滞后态，筛选同一窗口仍能看到它
            watermark = store.find("watermark_track", int(receipt.get("watermark_id") or 0))
            if watermark is not None:
                watermark["status"] = "滞后"
                watermark["pending"] = True
                watermark["abnormal"] = True

        receipt["status"] = "已应用"
        receipt["更新时间"] = datetime.now().isoformat()
        return result

    def recheck_entry(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """重新检查：消费该规则主题下未应用的水位纠正回执；没有回执时按当前水位全量重检。"""
        rule = store.find(MODULE, entry_id)
        if rule is None:
            return None, f"质量规则 {entry_id} 不存在或已归档"
        topic = rule.get("监控主题")
        pending_receipts = [
            row for row in store.rows("watermark_correction")
            if row.get("数据主题") == topic and row.get("status") != "已应用"
        ]
        if pending_receipts and rule.get("status") != "已生效":
            return None, "规则未生效，无法消费水位纠正回执"

        applied: list[str] = []
        windows_total = 0
        violations_total = 0
        for receipt in pending_receipts:
            result = self.apply_correction_receipt(receipt)
            if result is not None:
                applied.append(str(receipt.get("回执编号")))
                windows_total += len(result.get("受影响窗口") or [])
                violations_total += len(result.get("violations") or [])

        if not pending_receipts:
            watermark = self._find_watermark(topic)
            if watermark is None:
                return None, f"主题 {topic} 未配置水位追踪"
            check = self._run_quality_check(rule, watermark)
            return {
                "mode": "全量检查",
                "applied_receipts": [],
                "rechecked_windows": 0,
                "violations": len(check["violations"]),
            }, "无待消费的水位纠正回执，已按当前水位重新检查"

        return {
            "mode": "回执重检",
            "applied_receipts": applied,
            "rechecked_windows": windows_total,
            "violations": violations_total,
        }, f"已消费 {len(applied)} 份水位纠正回执，重新检查 {windows_total} 个受影响窗口，发现 {violations_total} 项违规"

    # ───────────────────────────────
    # 内部辅助
    # ───────────────────────────────

    def _find_topic(self, topic_code: str) -> dict[str, Any] | None:
        for row in store.rows("topic_registry"):
            if row.get("主题编号") == topic_code:
                return row
        return None

    def _find_watermark(self, topic_code: str) -> dict[str, Any] | None:
        for row in store.rows("watermark_track"):
            if row.get("数据主题") == topic_code:
                return row
        return None

    def _find_active_rule(self, topic_code: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if row.get("监控主题") == topic_code and row.get("status") == "已生效":
                return row
        return None

    def _run_quality_check(self, entry: dict[str, Any], watermark: dict[str, Any]) -> dict[str, Any]:
        """执行质量检查，异常时产生死信记录。"""
        try:
            threshold = float(entry.get("告警阈值", "0"))
        except (ValueError, TypeError):
            threshold = 0.0
        try:
            late_count = float(watermark.get("延迟记录数", "0"))
        except (ValueError, TypeError):
            late_count = 0.0

        violations = []
        if late_count > threshold:
            violations.append({
                "type": "延迟超限",
                "detail": f"延迟记录 {int(late_count)} 超过阈值 {threshold}",
                "topic": entry.get("监控主题"),
            })
            # 产生死信
            self._create_dead_letter(entry, watermark, "延迟超限")

        result = {
            "rule_id": entry.get("id"),
            "rule_code": entry.get("规则编号"),
            "topic": entry.get("监控主题"),
            "threshold": threshold,
            "late_count": late_count,
            "violations": violations,
            "checked_at": datetime.now().isoformat(),
            "passed": len(violations) == 0,
        }
        self._ensure_artifact_table("quality_check_results")
        store.rows("quality_check_results").append(result)
        entry["_last_check"] = result
        return result

    def _create_dead_letter(self, entry: dict[str, Any], watermark: dict[str, Any], reason: str) -> None:
        dl_rows = store.rows("dead_letter")
        dl_id = max((int(r.get("id", 0)) for r in dl_rows), default=0) + 1
        dl = {
            "id": dl_id,
            "status": "待处置",
            "pending": True,
            "abnormal": True,
            "记录编号": f"DEAD-{dl_id:04d}",
            "原始主题": entry.get("监控主题"),
            "原始分区": watermark.get("分区编号", "0"),
            "失败原因": reason,
            "原始消息体": f"延迟记录数:{watermark.get('延迟记录数')}",
            "重试次数": "0",
            "入库时间": datetime.now().isoformat(),
            "处置状态": "待分析",
        }
        dl_rows.append(dl)

    def _create_dead_letter_for_receipt(
        self, rule: dict[str, Any], receipt: dict[str, Any], late_total: float
    ) -> None:
        """回执重检触发的死信：按回执编号+检查版本去重，避免重复写入。"""
        dl_rows = store.rows("dead_letter")
        marker = f"回执:{receipt.get('回执编号')}"
        version_marker = f"检查版本:{receipt.get('检查版本')}"
        for row in dl_rows:
            body = str(row.get("原始消息体") or "")
            if marker in body and version_marker in body:
                return
        dl_id = max((int(r.get("id", 0)) for r in dl_rows), default=0) + 1
        dl_rows.append({
            "id": dl_id,
            "status": "待处置",
            "pending": True,
            "abnormal": True,
            "记录编号": f"DEAD-{dl_id:04d}",
            "原始主题": rule.get("监控主题"),
            "原始分区": receipt.get("分区编号", "0"),
            "失败原因": "延迟超限（水位纠正重检）",
            "原始消息体": (
                f"{marker} {version_marker} 纠正后延迟记录数:{int(late_total)} "
                f"受影响窗口:{len(receipt.get('受影响窗口') or [])}"
            ),
            "重试次数": "0",
            "入库时间": datetime.now().isoformat(),
            "处置状态": "待分析",
        })

    def get_quality_results(self, entry_id: int) -> list[dict[str, Any]]:
        self._ensure_artifact_table("quality_check_results")
        return [r for r in store.rows("quality_check_results") if r.get("rule_id") == entry_id]

    def _ensure_artifact_table(self, name: str) -> None:
        store.rows(name)
