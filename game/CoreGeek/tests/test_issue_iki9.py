"""issue IKIAE6 回归（2026-09-24，仅保留 **宝藏** 部分）。

召唤失败（结果 2/3）→ 记录失败坐标 + 立即用全部传闻重推 + 拒绝失败点计划。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.treasure import TreasurePlanner

config.HARDCODED_ASSIST = True


class TestTreasureFailureFeedback(unittest.TestCase):
    """IKIAE6：召唤结果 2（地点不对）→ 记录失败点 + 重推。"""

    def _planner(self):
        tp = TreasurePlanner()
        tp.observe("传闻一：石门在东方，祭坛隐于市")
        self.assertTrue(tp.apply_llm(
            '{"x": 20, "y": 16, "items": ["StarSand"], "day": 3, "ready": true}'))
        return tp

    def test_failure_records_site_and_reinfers(self):
        tp = self._planner()
        tp.mark_inferred()
        self.assertFalse(tp.needs_inference())
        tp.on_summon_result(2)
        self.assertIn((20, 16), tp.failed_sites)
        self.assertFalse(tp.plan.ready)
        self.assertTrue(tp.needs_inference(), "失败后应可重推（不等新传闻）")

    def test_failed_site_rejected(self):
        tp = self._planner()
        tp.on_summon_result(2)
        self.assertFalse(
            tp.apply_llm('{"x": 20, "y": 16, "items": ["StarSand"], "day": 3, "ready": true}'),
            "已失败的坐标不应再被接受")

    def test_new_site_accepted(self):
        tp = self._planner()
        tp.on_summon_result(2)
        self.assertTrue(tp.apply_llm(
            '{"x": 5, "y": 5, "items": ["StarSand"], "day": 4, "ready": true}'))

    def test_prompt_has_failure_feedback(self):
        tp = self._planner()
        tp.on_summon_result(2)
        text = tp.prompt(41, 32)
        self.assertIn("失败反馈", text)
        self.assertIn("(20,16)", text)

    def test_success_stops(self):
        tp = self._planner()
        tp.on_summon_result(1)
        self.assertTrue(tp.attempted)
        self.assertEqual(tp.failed_sites, [])


if __name__ == "__main__":
    unittest.main()
