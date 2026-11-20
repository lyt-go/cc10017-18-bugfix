"""水位纠正回执测试：倒退检测、向前修正、幂等恢复、冲突取舍与跨模块交付。

缺陷背景：消费偏移前进后事件时间水位错误倒退，质量规则漏掉真实滞后分区。
本文件覆盖修复后的关键行为：
- 生成水位纠正回执并保留纠正前后水位、旧检查版本与受影响窗口；
- 事件时间水位只能按检查版本向前修正，最后成功位置避免重复写入；
- 写入中断后可恢复；旧检查版本与新消费偏移冲突时只保留一个当前水位；
- 回执从水位追踪交付流式质量（保留当前版本），重新检查受影响窗口。
"""
from __future__ import annotations

import itertools
import unittest
from typing import Any

from app.services.stream_quality import StreamQualityService
from app.services.watermark_correction import WatermarkCorrectionService
from app.services.watermark_track import WatermarkTrackService
from app.store import store

_seq = itertools.count(1)


def _topic_code(prefix: str = "TOPI-C") -> str:
    return f"{prefix}{next(_seq):03d}"


class TestWatermarkCorrection(unittest.TestCase):
    def setUp(self) -> None:
        self.correction_svc = WatermarkCorrectionService()
        self.watermark_svc = WatermarkTrackService()
        self.quality_svc = StreamQualityService()

    # ───────────────────────────────
    # 测试夹具
    # ───────────────────────────────

    def _register_topic(self, code: str) -> None:
        rows = store.rows("topic_registry")
        rows.append({
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "status": "已上线", "pending": False, "abnormal": False,
            "主题编号": code, "主题名称": f"纠正测试主题{code}", "消息格式": "JSON",
        })

    def _make_inverted_watermark(
        self, topic: str, *, partition: str = "0", late: str = "12"
    ) -> dict[str, Any]:
        """造一条颠倒记录：消费偏移前进，事件时间水位从上一检查版本倒退。"""
        entry, missing = self.watermark_svc.create_entry({
            "标记编号": f"WATE-{topic}-{partition}",
            "数据主题": topic,
            "分区编号": partition,
            "事件时间水位": "2026-11-16T10:00:00",
            "处理时间水位": "2026-11-16T10:35:00",
            "延迟记录数": late,
            "消费偏移": "1048576",
            "检查时间": "2026-11-16T10:35:00",
            "检查版本": "v4",
        })
        self.assertFalse(missing)
        assert entry is not None
        entry["inverted"] = True
        entry["_prev_check"] = {
            "检查版本": "v3",
            "事件时间水位": "2026-11-16T10:30:00",
            "处理时间水位": "2026-11-16T10:32:00",
            "消费偏移": "1048000",
        }
        return entry

    def _make_rule(
        self, topic: str, *, threshold: str = "50", window: str = "5min", activate: bool = True
    ) -> dict[str, Any]:
        entry, missing = self.quality_svc.create_entry({
            "规则编号": f"STRE-{topic}", "监控主题": topic, "校验类型": "延迟校验",
            "告警阈值": threshold, "采样比例": "1.0", "检测窗口": window,
            "阻断动作": "告警", "规则版本": "v1",
        })
        self.assertFalse(missing)
        assert entry is not None
        if activate:
            self.quality_svc.run_action(entry["id"], "提交校验")
            activated, msg = self.quality_svc.run_action(entry["id"], "标记生效")
            self.assertIsNotNone(activated, msg)
        return entry

    def _receipts_of(self, topic: str, partition: str = "0") -> list[dict[str, Any]]:
        return [
            row for row in store.rows("watermark_correction")
            if row.get("数据主题") == topic and row.get("分区编号") == partition
        ]

    # ───────────────────────────────
    # 缺陷现场与纠正主链路
    # ───────────────────────────────

    def test_seed_record_is_inverted(self) -> None:
        """种子数据里订单主题分区0就是颠倒记录：偏移前进、事件时间水位倒退。"""
        seed = store.find("watermark_track", 1)
        self.assertEqual(seed["数据主题"], "TOPI-0001")
        self.assertTrue(seed["inverted"])
        prev = seed["_prev_check"]
        self.assertEqual(prev["检查版本"], "v3")
        self.assertLess(seed["事件时间水位"], prev["事件时间水位"])
        self.assertGreater(int(seed["消费偏移"]), int(prev["消费偏移"]))

    def test_correct_generates_receipt_and_rechecks_windows(self) -> None:
        """纠正生成回执：保留纠正前后水位、旧检查版本与受影响窗口，并交付重检。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        rule = self._make_rule(topic)

        receipt, message = self.correction_svc.correct_entry(entry)

        self.assertIsNotNone(receipt, message)
        assert receipt is not None
        # 回执保留纠正前后水位与旧检查版本
        self.assertEqual(receipt["旧检查版本"], "v4")
        self.assertEqual(receipt["检查版本"], "v5")
        self.assertEqual(receipt["纠正前事件时间水位"], "2026-11-16T10:00:00")
        self.assertEqual(receipt["纠正后事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(receipt["纠正前处理时间水位"], "2026-11-16T10:35:00")
        self.assertEqual(receipt["版本"], 1)
        self.assertEqual(receipt["status"], "已应用")
        self.assertEqual(receipt["最后成功位置"], "v5@1048576")
        # 受影响窗口按检测窗口 5min 切分
        windows = receipt["受影响窗口"]
        self.assertEqual(len(windows), 6)
        self.assertEqual(windows[0], "2026-11-16T10:00:00/2026-11-16T10:05:00")
        self.assertTrue(windows[-1].endswith("2026-11-16T10:30:00"))
        # 水位标记已向前修正，颠倒标记清除
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(entry["检查版本"], "v5")
        self.assertFalse(entry["inverted"])
        # 跨模块交付保留当前版本：流式质量按同一检查版本重检受影响窗口
        results = [r for r in store.rows("quality_check_results") if r.get("rule_id") == rule["id"] and r.get("recheck")]
        self.assertEqual(len(results), 1)
        recheck = results[0]
        self.assertEqual(recheck["检查版本"], "v5")
        self.assertEqual(recheck["受影响窗口"], windows)
        self.assertFalse(recheck["passed"])
        self.assertEqual(recheck["late_count"], 84.0)  # 12 × (6 窗口 + 1)
        # 真实滞后分区不再漏报：水位标记回到滞后态，死信落地
        self.assertEqual(entry["status"], "滞后")
        self.assertTrue(entry["abnormal"])
        dead_letters = [r for r in store.rows("dead_letter") if receipt["回执编号"] in str(r.get("原始消息体"))]
        self.assertEqual(len(dead_letters), 1)

    def test_correct_is_idempotent_no_duplicate(self) -> None:
        """重复纠正不重复写回执：同一分区只保留一份回执。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        self._make_rule(topic)

        receipt, _ = self.correction_svc.correct_entry(entry)
        self.assertIsNotNone(receipt)
        results_before = len(store.rows("quality_check_results"))
        dead_before = len(store.rows("dead_letter"))

        again, message = self.correction_svc.correct_entry(entry)
        self.assertIsNone(again)
        self.assertIn("无需纠正", message)
        self.assertEqual(len(self._receipts_of(topic)), 1)
        self.assertEqual(len(store.rows("quality_check_results")), results_before)
        self.assertEqual(len(store.rows("dead_letter")), dead_before)

    def test_lagging_partition_visible_after_correction(self) -> None:
        """纠正后筛选同一分区与同一窗口：滞后分区没有消失。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic, partition="7")
        rule = self._make_rule(topic)
        receipt, _ = self.correction_svc.correct_entry(entry)
        self.assertIsNotNone(receipt)

        rows, total = self.watermark_svc.list_entries(filters={"分区编号": "7"}, page=1, size=20)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["status"], "滞后")

        window = receipt["受影响窗口"][1]  # 任选一个受影响窗口
        results = self.quality_svc.get_quality_results(rule["id"])
        matched = [r for r in results if window in (r.get("受影响窗口") or [])]
        self.assertEqual(len(matched), 1)
        self.assertFalse(matched[0]["passed"])

    # ───────────────────────────────
    # 向前修正与中断恢复
    # ───────────────────────────────

    def test_report_regressed_reading_skipped_by_position(self) -> None:
        """上报倒退读数：被最后成功位置覆盖，跳过重复写入，水位不倒退。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        self._make_rule(topic)
        self.correction_svc.correct_entry(entry)
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:30:00")

        _, receipt, message = self.correction_svc.report_reading(entry, {
            "事件时间水位": "2026-11-16T10:10:00",
            "消费偏移": "1049000",
        })
        self.assertIsNone(receipt)
        self.assertIn("跳过重复写入", message)
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(entry["检查版本"], "v5")
        self.assertEqual(len(self._receipts_of(topic)), 1)

    def test_report_forward_reading_accepted(self) -> None:
        """正常前进读数直接接受，检查版本向前推进。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        self._make_rule(topic)
        self.correction_svc.correct_entry(entry)

        _, receipt, message = self.correction_svc.report_reading(entry, {
            "事件时间水位": "2026-11-16T10:40:00",
            "处理时间水位": "2026-11-16T10:45:00",
            "消费偏移": "1049000",
            "延迟记录数": "3",
        })
        self.assertIsNone(receipt)
        self.assertIn("正常前进", message)
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:40:00")
        self.assertEqual(entry["检查版本"], "v6")
        self.assertEqual(entry["消费偏移"], "1049000")
        self.assertEqual(entry["_prev_check"]["检查版本"], "v5")

    def test_resume_interrupted_receipt(self) -> None:
        """写入中断后恢复：按最后成功位置续写，重复恢复不产生重复写入。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        rule = self._make_rule(topic)

        # 模拟写入中断：回执已生成但尚未写回水位标记
        receipt, _ = self.correction_svc._upsert_receipt(
            entry=entry,
            old_version="v4",
            new_version="v5",
            prev_snapshot={"检查版本": "v4", "事件时间水位": "2026-11-16T10:00:00",
                           "处理时间水位": "2026-11-16T10:35:00", "消费偏移": "1048576"},
            reported_event="2026-11-16T10:00:00",
            corrected_event="2026-11-16T10:30:00",
            new_proc="2026-11-16T10:35:00",
            new_offset="1048576",
            reported_late="12",
            conflict="",
            candidates=["2026-11-16T10:00:00", "2026-11-16T10:30:00"],
        )
        self.assertEqual(receipt["status"], "写入中")
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:00:00")

        resumed = self.correction_svc.resume()
        self.assertIn(receipt, resumed)
        self.assertEqual(receipt["status"], "已应用")
        self.assertEqual(receipt["最后成功位置"], "v5@1048576")
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(entry["检查版本"], "v5")
        results = [r for r in store.rows("quality_check_results") if r.get("rule_id") == rule["id"] and r.get("recheck")]
        self.assertEqual(len(results), 1)

        # 重复恢复：没有新的写入
        results_before = len(store.rows("quality_check_results"))
        dead_before = len(store.rows("dead_letter"))
        self.correction_svc.resume()
        self.assertEqual(len(store.rows("quality_check_results")), results_before)
        self.assertEqual(len(store.rows("dead_letter")), dead_before)
        self.assertEqual(len(self._receipts_of(topic)), 1)

    # ───────────────────────────────
    # 冲突取舍与交付
    # ───────────────────────────────

    def test_conflict_keeps_single_current_watermark_and_versions(self) -> None:
        """旧检查版本与新消费偏移相遇：回执只保留一个当前水位，冲突产生新版本。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        rule = self._make_rule(topic)
        self.correction_svc.correct_entry(entry)
        self.assertEqual(entry["检查版本"], "v5")

        # 旧检查版本 v3 的读数带着新消费偏移到来：按新偏移重算的水位倒退
        _, receipt, message = self.correction_svc.report_reading(entry, {
            "事件时间水位": "2026-11-16T10:10:00",
            "消费偏移": "1049000",
            "基准版本": "v3",
        })
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertIn("版本冲突", message)
        # 只保留一个当前水位：向前修正保留较大水位，当前版本不变
        self.assertEqual(receipt["检查版本"], "v5")
        self.assertEqual(receipt["纠正后事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(receipt["候选水位"], ["2026-11-16T10:10:00", "2026-11-16T10:30:00"])
        self.assertIn("基准版本 v3", receipt["冲突判定"])
        self.assertEqual(receipt["版本"], 2)  # 同一回执产生新版本
        self.assertEqual(entry["事件时间水位"], "2026-11-16T10:30:00")
        self.assertEqual(entry["检查版本"], "v5")

        # 再次冲突：仍是同一份回执，版本继续推进，不重复显示
        _, receipt2, _ = self.correction_svc.report_reading(entry, {
            "事件时间水位": "2026-11-16T10:05:00",
            "消费偏移": "1049500",
            "基准版本": "v3",
        })
        self.assertIs(receipt2, receipt)
        self.assertEqual(receipt["版本"], 3)
        self.assertEqual(receipt["status"], "已应用")
        self.assertEqual(len(self._receipts_of(topic)), 1)
        # 同一检查版本不重复重检
        results = [r for r in store.rows("quality_check_results") if r.get("rule_id") == rule["id"] and r.get("recheck")]
        self.assertEqual(len(results), 1)

    def test_receipt_delivery_pending_until_rule_active_then_recheck(self) -> None:
        """规则未生效时回执停留已交付；规则生效后重新检查消费回执。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic)
        rule = self._make_rule(topic, activate=False)

        receipt, _ = self.correction_svc.correct_entry(entry)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt["status"], "已交付")

        self.quality_svc.run_action(rule["id"], "提交校验")
        activated, msg = self.quality_svc.run_action(rule["id"], "标记生效")
        self.assertIsNotNone(activated, msg)

        summary, message = self.quality_svc.recheck_entry(rule["id"])
        self.assertIsNotNone(summary, message)
        assert summary is not None
        self.assertEqual(summary["applied_receipts"], [receipt["回执编号"]])
        self.assertEqual(summary["rechecked_windows"], 6)
        self.assertEqual(summary["violations"], 1)
        self.assertEqual(receipt["status"], "已应用")
        # 回执全程保留当前版本
        self.assertEqual(receipt["检查版本"], "v5")

    # ───────────────────────────────
    # 查询口径：检索、排序、翻页
    # ───────────────────────────────

    def test_receipt_list_filter_sort_paginate(self) -> None:
        """回执列表可按条件检索、排序和翻页，查询结果保留过滤口径。"""
        topics = [_topic_code("TOPI-PG") for _ in range(3)]
        for index, topic in enumerate(topics):
            self._register_topic(topic)
            entry = self._make_inverted_watermark(topic, partition=str(index))
            self._make_rule(topic)
            self.correction_svc.correct_entry(entry)

        # 条件检索：按数据主题前缀过滤
        rows, total = self.correction_svc.list_receipts(filters={"数据主题": "TOPI-PG"})
        self.assertEqual(total, 3)
        # 排序：按分区编号降序
        rows, _ = self.correction_svc.list_receipts(
            filters={"数据主题": "TOPI-PG"}, sort="分区编号", order="desc")
        self.assertEqual([row["分区编号"] for row in rows], ["2", "1", "0"])
        # 翻页：保留过滤口径
        page1, total1 = self.correction_svc.list_receipts(
            filters={"数据主题": "TOPI-PG"}, sort="分区编号", order="asc", page=1, size=2)
        page2, total2 = self.correction_svc.list_receipts(
            filters={"数据主题": "TOPI-PG"}, sort="分区编号", order="asc", page=2, size=2)
        self.assertEqual((total1, total2), (3, 3))
        self.assertEqual([row["分区编号"] for row in page1], ["0", "1"])
        self.assertEqual([row["分区编号"] for row in page2], ["2"])
        # 关键字检索：按回执编号
        receipt_code = page1[0]["回执编号"]
        rows, total = self.correction_svc.list_receipts(keyword=receipt_code)
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["回执编号"], receipt_code)

    def test_watermark_list_inverted_filter_and_sort(self) -> None:
        """水位追踪列表可按分区定位颠倒记录，并支持排序翻页。"""
        topic = _topic_code()
        self._register_topic(topic)
        entry = self._make_inverted_watermark(topic, partition="9")

        rows, _ = self.watermark_svc.list_entries(inverted=True, page=1, size=100)
        self.assertTrue(all(row.get("inverted") for row in rows))
        self.assertIn(entry["id"], [row["id"] for row in rows])

        rows, total = self.watermark_svc.list_entries(
            filters={"分区编号": "9"}, inverted=True, sort="检查版本", order="desc")
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]["id"], entry["id"])


if __name__ == "__main__":
    unittest.main()
