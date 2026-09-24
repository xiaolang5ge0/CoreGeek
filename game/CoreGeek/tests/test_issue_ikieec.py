"""issue IKIEEC 参考文档（《宝藏 Prompt 构造详解》）回归 —— 2026-09-24。

可借鉴点落地：
1. 传闻**全量**注入（不再只取最近 12 条）；
2. 计划校验第 6 条：**开启日不得早于当前天数**（否则永远打不开 → 丢弃重推）；
3. 执行：**提前 1 天**移动到祭坛（开启日当天已在旁边，不浪费赶路回合）；
4. 日志可读性：`reason` 放在 JSON **最后**（避免 400 字截断吃掉 x/y/day 字段）；
5. prompt 里带上**当前天数**（LLM 才能给出不早于今天的开启日）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx
from agent.planners.treasure import TreasurePlanner
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

PIONEER = 10011


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={}, shop=(25, 20))
    base.update(kw)
    return SimWorld(**base)


class TestPromptFullLegends(unittest.TestCase):
    def test_all_legends_injected(self):
        tp = TreasurePlanner()
        for i in range(20):
            tp.observe("传闻%d：石门线索%d" % (i, i))
        text = tp.prompt(41, 32, 3)
        self.assertIn("传闻0：石门线索0", text, "最早的传闻也必须在 prompt 里")
        self.assertIn("传闻19：石门线索19", text)

    def test_prompt_carries_current_day(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门，门需三钥")
        self.assertIn("第 6 天", tp.prompt(41, 32, 6))

    def test_reason_last_in_schema(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        text = tp.prompt(41, 32, 1)
        schema = text[text.rindex("只返回 JSON"):]
        self.assertLess(schema.index('"x"'), schema.index('"reason"'),
                        "reason 应在 JSON 最后（避免日志截断吃掉坐标）")


class TestPlanDayValidation(unittest.TestCase):
    def test_past_day_rejected(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        self.assertFalse(tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["AcientTablet"], "day": 3, "ready": true}',
            current_day=7), "开启日早于当前天数 → 永远打不开，必须丢弃")

    def test_today_or_future_day_accepted(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        self.assertTrue(tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["AcientTablet"], "day": 7, "ready": true}',
            current_day=7))
        tp2 = TreasurePlanner()
        tp2.observe("传闻：西部有一石门")
        self.assertTrue(tp2.apply_llm(
            '{"x": 4, "y": 4, "items": ["AcientTablet"], "day": 9, "ready": true}',
            current_day=7))

    def test_no_current_day_keeps_backward_compatible(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        self.assertTrue(tp.apply_llm(
            '{"x": 5, "y": 5, "items": ["AcientTablet"], "day": 2, "ready": true}'))


class TestTravelOneDayEarly(unittest.TestCase):
    def _turn(self, round_no):
        sim = make_sim(gold=100)
        sim.round_no = round_no
        sim.role(PIONEER)["backpack"] = ["AcientTablet"]
        sim.role(PIONEER)["pos"] = {"x": 30, "y": 20}   # 远离祭坛 (3,3)
        return Turn.load(sim.payload())

    def _planned(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["AcientTablet"], "day": 5, "ready": true}',
            current_day=1)
        return tp

    def test_moves_day_before_open(self):
        tp = self._planned()
        turn = self._turn(3 * 130 + 20)          # 第 4 天 = 开启日 5 的前一天
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        ctx = _Ctx({})
        ctx.reserved = set()
        cmd = tp.cmd(turn, pioneer, Pos(25, 20), ctx)
        self.assertIsNotNone(cmd, "开启日前 1 天应开始赶路")
        self.assertEqual(cmd["action"], "move")

    def test_waits_before_day_minus_one(self):
        tp = self._planned()
        turn = self._turn(2 * 130 + 20)          # 第 3 天 < 开启日-1
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        ctx = _Ctx({})
        ctx.reserved = set()
        self.assertIsNone(tp.cmd(turn, pioneer, Pos(25, 20), ctx))


if __name__ == "__main__":
    unittest.main()