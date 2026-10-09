"""IKKDR0 Q5 三项回归（2026-10-09）：
F. errorCode=2 重答 → 强制只重算（force_answer：cmd 被拒、prompt 带"只重算模式"）；
E. 任务枯竭检测（冷却 0 + isValid 恒 false >30 回合 → tasks_exhausted，开拓者宝藏转产）；
D. 宝藏 prompt 坐标系语义勘误（北=y+1）+ ts=2 后无新传闻也主动重推。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.planners.task import PlannerOutput, ST_LLM, ST_SUBMIT, TaskPlanner, TaskSession
from agent.planners.treasure import TreasurePlanner
from agent.protocol import Turn
from test_v2_rules import base_payload


class TestQ5FRefineOnly(unittest.TestCase):
    """errorCode=2 → force_answer：LLM 再返 cmd 被拒；prompt 含只重算说明。"""

    def _session_at_submit(self):
        s = TaskSession()
        s.task_text = "任务"
        s.answer = {"total_events": 7}
        s.stage = ST_SUBMIT
        s.submitted = True
        return s

    def test_error_code2_forces_refine_only(self):
        planner = TaskPlanner()
        s = self._session_at_submit()
        payload = base_payload(40)
        payload["phaseTask"] = "任务"      # work() 开头 phase_task 门控
        payload["errors"] = [{"errorCode": 2, "description": "键值比对不通过: $/total_events: 数值不符"}]
        turn = Turn.load(payload)
        out = planner.work(turn, s)
        self.assertTrue(s.force_answer, "errorCode=2 → 只重算模式")
        self.assertEqual(s.stage, "WAIT_LLM")   # work→_advance 当回合即发重答 prompt
        self.assertIn("只重算模式", out.prompt or "", "重答 prompt 应含只重算说明")

    def test_refine_rejects_new_commands(self):
        planner = TaskPlanner()
        s = self._session_at_submit()
        s.force_answer = True
        s.last_error = "$/total_events: 数值不符"
        s.stage = ST_LLM
        out = PlannerOutput()
        # LLM 不听话又返 cmd（无 answer）→ 拒绝、拉回 LLM 重发
        planner._on_llm_result(s, json.dumps({"cmd": "cat /tmp/x.md", "answer": "", "isFinished": False}))
        self.assertNotEqual(s.stage, "WAIT_CMD")
        self.assertEqual(s.stage, ST_LLM, "只重算模式下 cmd 应被拒绝")


class TestQ5ETasksExhausted(unittest.TestCase):
    """E：两任务点 isValid=false 且冷却 0 持续 >30 回合 → 枯竭；开拓者宝藏转产。"""

    @staticmethod
    def _dead_tasks():
        return [
            {"taskType": "自进化类1", "taskPosition": {"x": 14, "y": 14},
             "coldDownRounds": 0, "scoreReward": 0, "goldReward": 0,
             "isValid": False, "timeoutRounds": 0},
            {"taskType": "自进化类2", "taskPosition": {"x": 16, "y": 17},
             "coldDownRounds": 0, "scoreReward": 0, "goldReward": 0,
             "isValid": False, "timeoutRounds": 0},
        ]

    def test_exhausted_after_30_rounds(self):
        brain = Brain()
        p1 = base_payload(400)
        p1["teamOur"]["playerTasks"] = self._dead_tasks()   # 注意 base_payload 默认为空！
        brain.decide(p1)
        self.assertFalse(brain.tasks_exhausted, "首回合不判枯竭")
        p2 = base_payload(440)  # 40 回合后仍全失效
        p2["teamOur"]["playerTasks"] = self._dead_tasks()
        r2, t2 = brain.decide(p2)
        self.assertTrue(brain.tasks_exhausted, "持续 >30 回合 → 枯竭")
        self.assertIn("tasks_exhausted", t2)

    def test_revive_on_valid_task(self):
        brain = Brain()
        brain.tasks_exhausted = True
        brain._tasks_dead_since = 100
        p = base_payload(140)
        p["teamOur"]["playerTasks"] = [
            {"taskType": "自进化类1", "taskPosition": {"x": 14, "y": 14},
             "coldDownRounds": 0, "scoreReward": 120, "goldReward": 120,
             "isValid": True, "timeoutRounds": 15},
        ]
        turn = Turn.load(p)
        trace = {}
        brain._learn_tasks_exhausted(turn, trace)   # 直接单测学习函数
        self.assertFalse(brain.tasks_exhausted, "出现有效任务应复位")
        self.assertIn("tasks_revived", trace)

    def test_pioneer_treasure_overrides_when_exhausted(self):
        # 枯竭时宝藏门控豁免"炮塔未全 L3 且买得起券"的前置
        from agent.fsm_pioneer import PioneerFSM
        payload = base_payload(140)
        payload["teamOur"]["roles"] = [
            r for r in payload["teamOur"]["roles"] if r.get("roleType") != "rocket"]
        payload["mapInfo"]["zones"] = [
            z for z in payload["mapInfo"]["zones"] if z["neutralType"] in ("vendor", "weaponShop")]
        turn = Turn.load(payload)
        fsm = PioneerFSM()
        ctx = type("C", (), {"treasure": TreasurePlanner(), "tasks_exhausted": True,
                             "reserved": frozenset(), "note": staticmethod(lambda *a, **k: None)})()
        ctx.treasure.plan = ctx.treasure.plan.__class__(
            __import__("agent.protocol", fromlist=["Pos"]).Pos(20, 19),
            ("IronWhistle",), 6, True, "test")
        self.assertTrue(fsm._treasure_ready(turn, turn.pioneer(), ctx),
                        "任务枯竭 → 宝藏门控豁免炮塔前置")


class TestQ5DTreasureCoords(unittest.TestCase):
    """D：prompt 坐标系勘误（北=y+1）+ ts=2 后无新传闻也重推。"""

    def test_prompt_has_correct_axis_semantics(self):
        tp = TreasurePlanner().prompt(41, 32, 5, zones="石矿(0,12)")
        self.assertIn("之北 = y+1", tp)
        self.assertIn("原点在**左下角", tp)
        self.assertNotIn("北部 → y 取北侧小值", tp, "旧的错误方向提示必须移除")
        self.assertIn("(20,19)", tp, "应给出『之东两格之北一格』的正确换算示例")

    def test_failed_site_feedback_warns_axis_flip(self):
        tp = TreasurePlanner()
        tp.failed_sites.append((20, 17))
        tp.legends.append("祭坛在石门之北")
        text = tp.prompt(41, 32, 5)
        self.assertIn("y 轴方向此前算反", text, "失败反馈应提示方向勘误")

    def test_repush_without_new_folk(self):
        brain = Brain()
        # d1：有传闻 → 推断一次
        p1 = base_payload(40)
        p1["worldNews"]["folkLegends"] = "祭坛石门需要一把回音铁哨"
        r1, t1 = brain.decide(p1)
        self.assertIn("treasure_llm", t1)
        # r1 的 llmResp 回复 → 计划就绪；r2 模拟召唤失败（ts=2）
        from agent.protocol import Pos
        brain.treasure.plan = brain.treasure.plan.__class__(
            Pos(20, 17), ("IronWhistle",), 5, True, "test")
        brain.treasure.on_summon_result(2)
        # d1 后续回合：**无新传闻** → 也应主动重推
        p2 = base_payload(42)
        p2["worldNews"]["folkLegends"] = ""   # 无新传闻
        r2, t2 = brain.decide(p2)
        self.assertIn("treasure_llm", t2, "ts=2 后无新传闻也应主动重推")
        self.assertIn("y 轴方向此前算反", r2.get("prompt") or "",
                      "重推 prompt 应带失败反馈+方向勘误")


if __name__ == "__main__":
    unittest.main()
