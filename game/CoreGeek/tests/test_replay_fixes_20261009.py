"""32进16 首战复盘修复回归（2026-10-09 用户裁决项）。

覆盖：
- P0-1 火箭冷却账本：同炮间隔 <5 不再开火（平台 cooldown 字段恒 0 不可信）。
- 落点补齐：targetPos 数量必须=武器等级（接口 §2.2；残局 1-2 敌人时补邻近空地）。
- P0-2 答案结构校验（方案乙）：{"errors":[...]} 放行（旧版 "error" 子串误杀回归）；
  纯报错报文拦截；全零拦截。
- P1-8 ST_CONFIRM：提交后确认期内绝不重复提交；completed/SOP 延迟到确认成功；
  平台撤任务（phaseTask 清空）时也能补跑确认。
- P0-3 双保险：任务活跃冻结宝藏/新闻 LLM；通道归属 mismatch 不互相喂响应；
  llm_resp_ok=False 时任务不消费外来 llmResp 且不烧预算。
"""
import json
import unittest
from types import SimpleNamespace

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.protocol import Turn
from agent.brain import Brain
from agent.planners.task import (
    TaskPlanner, TaskSession, ST_SUBMIT, ST_CONFIRM, ST_DONE, ST_LLM, ST_WAIT_LLM,
)
from agent.fire import JointFirePlanner


def _turn(sim: SimWorld, round_no: int | None = None) -> Turn:
    payload = sim.payload()
    if round_no is not None:
        payload["roundNo"] = round_no
    return Turn.load(payload)


class TestFireLedgerAndPad(unittest.TestCase):
    """P0-1 冷却账本 + targetPos 补齐到武器等级。"""

    def setUp(self):
        # pioneer(9,24)；注入 2 级火箭于 (10,23)（距开拓者切比雪夫 1）；1 个敌人
        self.sim = SimWorld(station_pos=(10, 24))
        self.sim.roles.append(
            self.sim._role(10040, 10, 23, "rocket", 1000, level=2)
        )
        self.sim.spawn_robot(15, 24, "smallRobot", hp=40, rid=30001)
        self.planner = JointFirePlanner()

    def _fire(self, round_no: int) -> dict:
        turn = _turn(self.sim, round_no)
        pioneer = turn.pioneer()
        commands: dict = {}
        self.planner.plan(turn, pioneer, commands, {})
        return commands

    def test_targets_padded_to_weapon_level(self):
        """2 级炮 + 仅 1 个敌人：targetPos 必须补齐到 2 个（否则接口判非法）。"""
        commands = self._fire(1)
        self.assertIn(10040, commands)
        self.assertEqual(len(commands[10040]["targetPos"]), 2)

    def test_no_refire_within_cooldown_gap(self):
        """同炮间隔 <5：账本拦截（实测间隔 ≤4 全失败）。"""
        self._fire(1)
        for r in (2, 3, 4, 5):
            commands = self._fire(r)
            self.assertNotIn(10040, commands, f"round {r} 不应再开火")
        commands = self._fire(6)
        self.assertIn(10040, commands, "间隔 ≥5 应恢复开火")

    def test_fail_streak_skips_weapon(self):
        """连续 2 次反馈失败 → 跳过该炮（换炮）。"""
        self._fire(1)
        # 下一回合反馈该炮失败（fb_fail 含 10040）
        payload = self.sim.payload()
        payload["roundNo"] = 2
        payload["lastRoundRoleActionResults"] = {"10040": False}
        turn = Turn.load(payload)
        self.planner.plan(turn, turn.pioneer(), {}, {})
        # 再失败一次 → streak=2
        payload["roundNo"] = 3
        turn = Turn.load(payload)
        self.planner.plan(turn, turn.pioneer(), {}, {})
        self.assertEqual(self.planner._fail_streak.get(10040), 2)
        commands = self._fire(4)
        self.assertNotIn(10040, commands, "连败 2 次后应跳过该炮")


class TestAnswerStructure(unittest.TestCase):
    """P0-2 方案乙：结构校验，不做关键词子串否决。"""

    def setUp(self):
        self.planner = TaskPlanner()

    def test_errors_key_is_valid_answer(self):
        """实战 F7 回归：答案 key "errors" 含子串 "error"，旧版永不提交死循环。"""
        self.assertFalse(self.planner._answer_suspect({"errors": [1, 2]}))
        self.assertFalse(self.planner._answer_suspect('{"errors": [{"code": "401"}]}'))

    def test_pure_error_payload_blocked(self):
        """整体是报错报文（键全为报错键、无答案键）→ 拦。"""
        self.assertTrue(self.planner._answer_suspect({"error": "not found"}))
        self.assertTrue(self.planner._answer_suspect(
            '{"status": "error", "message": "unauthorized"}'))

    def test_normal_and_allzero(self):
        self.assertFalse(self.planner._answer_suspect({"token": "abc123"}))
        self.assertTrue(self.planner._answer_suspect({"total_count": 0, "types": []}))
        self.assertTrue(self.planner._answer_suspect(""))


class TestSubmitConfirm(unittest.TestCase):
    """P1-8：ST_CONFIRM 提交确认期。"""

    def _submit_once(self):
        sim = SimWorld()
        sim.phase_task = "T"
        planner = TaskPlanner()
        session = TaskSession()
        session.task_text = "T"
        session.answer = '{"token": "abc12345"}'
        session.stage = ST_SUBMIT
        out = planner.work(_turn(sim, 1), session)
        return planner, session, out, sim

    def test_confirm_stage_no_resubmit(self):
        """提交 → ST_CONFIRM；确认期内绝不重复提交；2 回合后确认完成。"""
        planner, session, out, sim = self._submit_once()
        self.assertIsNotNone(out.submit)
        self.assertEqual(session.stage, ST_CONFIRM)
        self.assertEqual(planner.completed, 0, "completed 应延迟到确认")
        # 确认期第 1 回合：不重复提交
        out2 = planner.work(_turn(sim, 2), session)
        self.assertIsNone(out2.submit)
        self.assertEqual(session.stage, ST_CONFIRM)
        # 静默满 2 回合 → 确认完成 + SOP 沉淀
        out3 = planner.work(_turn(sim, 3), session)
        self.assertIsNone(out3.submit)
        self.assertEqual(session.stage, ST_DONE)
        self.assertEqual(planner.completed, 1)
        self.assertTrue(planner.sop)

    def test_confirm_on_task_withdrawn(self):
        """平台验收通过撤下任务（phaseTask 清空）→ 也要补跑确认，completed/SOP 不丢。"""
        planner, session, _, _ = self._submit_once()
        empty = SimWorld()                      # phase_task=""
        planner.work(_turn(empty, 2), session)
        self.assertEqual(planner.completed, 1)
        self.assertTrue(planner.sop)
        self.assertEqual(session.stage, TaskSession().stage)  # reset 后初始态

    def test_errorcode2_during_confirm_returns_to_llm(self):
        """确认期内收到 errorCode=2 → 重答（completed 不虚报）。"""
        planner, session, _, sim = self._submit_once()
        payload = sim.payload()
        payload["roundNo"] = 2
        payload["errors"] = [{"errorCode": 2, "description": "答案错误"}]
        out = planner.work(Turn.load(payload), session)
        # errorCode=2 拉回 ST_LLM 后 _advance 立即重发重答 prompt
        self.assertEqual(session.stage, ST_WAIT_LLM)
        self.assertTrue(out.prompt)
        self.assertEqual(planner.completed, 0)


class TestChannelOwner(unittest.TestCase):
    """P0-3 双保险：任务期冻结 + 通道归属仲裁。"""

    def test_task_freeze_blocks_treasure_prompt(self):
        """任务活跃（phaseTask 非空）→ 宝藏线索再足也不发宝藏 prompt。"""
        brain = Brain()
        brain.treasure.legends.append("传闻A")
        sim = SimWorld()
        payload = sim.payload()
        payload["phaseTask"] = "任务进行中"
        payload["worldNews"] = {"officialNews": "", "folkLegends": "宝藏藏于东岭"}
        ctx = SimpleNamespace(trace={}, prompt="")
        brain._record_news(Turn.load(payload), ctx)
        self.assertEqual(ctx.prompt, "", "任务期必须冻结宝藏 LLM")
        # 对照：非任务期会发（验证测试有效性）
        brain2 = Brain()
        brain2.treasure.legends.append("传闻A")
        payload2 = sim.payload()
        payload2["worldNews"] = {"officialNews": "", "folkLegends": "宝藏藏于东岭"}
        ctx2 = SimpleNamespace(trace={}, prompt="")
        brain2._record_news(Turn.load(payload2), ctx2)
        self.assertNotEqual(ctx2.prompt, "")

    def test_owner_mismatch_not_fed_to_treasure(self):
        """llmResp 归属任务但 _llm_waiting=treasure → 不喂给宝藏（F8 回归）。"""
        brain = Brain()
        brain._llm_waiting = "treasure"
        brain._llm_round = 5
        brain.llm_owner = "task"            # 上回合 prompt 被任务抢占

        applied = []

        class FakeTreasure:
            plan = SimpleNamespace(ready=False, location=None, items=[], day=0)

            def needs_inference(self) -> bool:
                return False   # 无新推断需求（只测 mismatch 不互喂）

            def apply_llm(self, *a, **k):
                applied.append(a)
                return True

            def take_await(self) -> bool:
                return False

            def mark_summon_sent(self) -> None:
                pass

        brain.treasure = FakeTreasure()
        sim = SimWorld()
        payload = sim.payload()
        payload["roundNo"] = 6
        payload["llmResp"] = json.dumps(
            {"cmd": "", "answer": '{"token": "x12345"}', "isFinished": True})
        out, trace = brain.decide(payload)
        self.assertFalse(applied, "任务响应绝不喂给宝藏")
        self.assertEqual(trace.get("llm_owner_mismatch", {}).get("waiting"), "treasure")

    def test_task_skips_foreign_llm_resp_without_budget(self):
        """llm_resp_ok=False：任务不消费外来 llmResp，退回 LLM_LOOP 不烧预算。"""
        sim = SimWorld()
        sim.phase_task = "T"
        payload = sim.payload()
        payload["llmResp"] = json.dumps(
            {"cmd": "", "answer": '{"token": "x12345"}', "isFinished": True})
        turn = Turn.load(payload)
        planner = TaskPlanner()
        session = TaskSession()
        session.task_text = "T"
        session.stage = ST_WAIT_LLM
        session.llm_pending = True
        session.llm_loops = 3
        session.llm_resp_ok = False         # brain 判定通道被宝藏占用
        planner.work(turn, session)
        # 不消费 → 回 ST_LLM → _advance 立即重发任务 prompt（重新发起请求）
        self.assertEqual(session.stage, ST_WAIT_LLM)
        self.assertEqual(session.llm_loops, 3, "回退-1 后重发+1：净不增=不烧预算")
        self.assertEqual(session.last_llm_raw, "", "未消费外来响应")
        self.assertTrue(session.llm_resp_ok, "消费位复位")
        # 对照：归属明确时正常消费（同一 payload，仅改回合号）
        session2 = TaskSession()
        session2.task_text = "T"
        session2.stage = ST_WAIT_LLM
        session2.llm_pending = True
        session2.llm_resp_ok = True
        payload2 = sim.payload()
        payload2["roundNo"] = 2
        payload2["llmResp"] = json.dumps(
            {"cmd": "", "answer": '{"token": "x12345"}', "isFinished": True})
        planner.work(Turn.load(payload2), session2)
        # 归属明确 → 正常消费并同回合提交（ST_SUBMIT 分支随即发 submit → ST_CONFIRM）
        self.assertEqual(session2.stage, ST_CONFIRM)
        self.assertTrue(session2.submitted)


if __name__ == "__main__":
    unittest.main()
