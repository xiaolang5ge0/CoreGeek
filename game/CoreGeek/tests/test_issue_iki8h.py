"""issue IKI8H9 / IKI8HA 回归（2026-09-24）：

1. 自进化任务：API 响应格式/分页/别写脆弱解析脚本写进 prompt；家族经验漏学引号版 Authorization；
   LLM 服务端故障（errorCode=3）不消耗循环预算；强制答案仅在截止回合才放弃。
2. 墙升级券：D4 前总持有上限 15（别屯券，全力升级炮台+围墙）。
3. 紧急抢修：L3 满级墙无法升级 → 用 WallFixer（用户："降级用围墙修复包才对"）。
4. 宝藏：低优先级——无任务可接 + 炮塔全 L3 才去；未到开启日不召唤。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import Brain, _Ctx
from agent.fsm_pioneer import PioneerFSM
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.planners.task import PlannerOutput, ST_DONE, ST_LLM, ST_WAIT_LLM, TaskPlanner, TaskSession
from agent.planners.treasure import TreasurePlanner
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1, W2, PIONEER = 10010, 10012, 10011


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestTaskPromptAndRobustness(unittest.TestCase):
    def test_api_contract_states_response_format(self):
        c = TaskPlanner._contract("api")
        self.assertIn("data.records", c)
        self.assertIn("total_count", c)
        self.assertIn("offset", c)
        self.assertIn("不要写复杂的 python 解析脚本", c)

    def test_learn_notes_quoted_authorization(self):
        """真实报文 `Expected format: 'Authorization: Bearer <api_key>'`（带引号）也要学到。"""
        p = TaskPlanner()
        s = TaskSession()
        s.task_type = "api"
        p._learn_notes(
            s,
            '{"status":"error","message":"Authentication failed: Missing \'Authorization\' header. '
            'Expected format: \'Authorization: Bearer <api_key>\'","code":401}',
        )
        self.assertIn("Bearer", "\n".join(p.notes["api"]))

    def test_learn_notes_quoted_param(self):
        p = TaskPlanner()
        s = TaskSession()
        s.task_type = "api"
        p._learn_notes(s, '{"message":"Missing required parameter: \'location\'"}')
        self.assertIn("location", "\n".join(p.notes["api"]))

    def test_llm_service_failure_not_counted(self):
        """errorCode=3（LLM 503/502）→ 不消耗循环预算、不算非 JSON（IKI8HA r29/r30）。"""
        sim = make_sim()
        sim.phase_task = "任务"
        sim.next_errors = [{"errorCode": 3, "description": "LLM 503"}]
        brain = Brain()
        s = brain.task_session
        s.task_text = "任务"
        s.stage = ST_WAIT_LLM
        s.llm_pending = True
        s.llm_loops = 3
        turn = Turn.load(sim.payload())
        brain.task_planner.work(turn, s)
        # 失败回合净增 0（旧行为：被当成答非 JSON → non_json+1 → 3 次即 ST_DONE）
        self.assertEqual(s.llm_loops, 3, "服务端故障回合不应净消耗循环预算")
        self.assertEqual(s.non_json, 0, "服务端故障不应算作非 JSON")
        self.assertNotEqual(s.stage, ST_DONE)

    def test_force_does_not_give_up_before_deadline(self):
        """force_sent 但未到截止回合 → 继续给 LLM 机会（不 ST_DONE）。"""
        sim = make_sim()
        sim.phase_task = "任务"
        brain = Brain()
        s = brain.task_session
        s.task_text = "任务"
        s.stage = ST_LLM
        s.force_sent = True
        s.max_loops = 0        # 触发 force
        s.timeout_rounds = 0   # 未到截止（_at_deadline=False）
        turn = Turn.load(sim.payload())
        out = brain.task_planner._advance(turn, s, PlannerOutput())
        self.assertNotEqual(s.stage, ST_DONE)
        self.assertTrue(out.prompt)


class TestWallVoucherCap(unittest.TestCase):
    def _plan(self, held):
        sim = make_sim()
        sim.round_no = 261   # D3 白天
        for y in (22, 23, 24, 25):   # FRONT(W) 列 L1 墙
            sim.roles.append(sim._role(40000 + y, 13, y, "wall", 1000, level=1))
        sim.role(W1)["backpack"] = ["WallUpgradeVoucher1"] * held
        turn = Turn.load(sim.payload())
        return UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")

    @staticmethod
    def _wall_stock(plan):
        return [m for m in plan if m.kind == "stock" and "WallUpgradeVoucher" in m.voucher]

    def test_cap_blocks_stock_at_15_before_d4(self):
        self.assertEqual(self._wall_stock(self._plan(15)), [],
                         "D4 前持券已达 15 → 不得再囤墙升级券")

    def test_stock_allowed_below_cap(self):
        self.assertTrue(self._wall_stock(self._plan(5)), "未到上限应允许按需求备货")


class TestCriticalRepair(unittest.TestCase):
    def _setup(self, level, hp, round_no=461):
        sim = make_sim()
        sim.roles.append(sim._role(40100, 13, 23, "wall", hp, level=level))
        sim.role(W1)["backpack"] = ["WallFixer"] * 5
        sim.round_no = round_no   # D4 夜
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.layout_anchor = (10, 23)
        ctx.layout_front = "W"
        ctx.home_anchor = Pos(10, 24)
        ctx.repair_anchor = Pos(10, 24)
        ctx.robot_cells = ()
        return turn, unit, fsm, ctx

    def test_l3_critical_wall_uses_wallfixer(self):
        """L3（满级，升级券无效）且血 <35% → 抢修（WallFixer）。"""
        turn, unit, fsm, ctx = self._setup(3, 200)   # 200/2000 = 10%
        cmd = fsm._critical_repair(turn, unit, ctx)
        self.assertIsNotNone(cmd, "L3 紧急受损墙应触发抢修")
        self.assertIn(cmd["action"], ("use", "move"))

    def test_l3_healthy_wall_not_repaired(self):
        turn, unit, fsm, ctx = self._setup(3, 1900)  # 95%
        self.assertIsNone(fsm._critical_repair(turn, unit, ctx))

    def test_upgrade_targets_include_critical_l3(self):
        turn, unit, fsm, ctx = self._setup(3, 200)
        targets = fsm._upgrade_targets(turn, ctx)
        self.assertTrue(any(t[0] == Pos(13, 23) for t in targets),
                        "满级但紧急受损的 L3 墙应进入修复目标（靠 WallFixer）")

    def test_night_flow_critical_repair_preempts_upgrade(self):
        """夜间：紧急抢修优先于升级任务（否则整夜升 L1 墙、L3 关键墙被打掉）。"""
        turn, unit, fsm, ctx = self._setup(3, 200)
        fsm.upgrade = (Pos(14, 22), "wall")   # 有个升级任务
        fsm.build = None
        cmd = fsm._repairer(turn, unit, ctx)
        self.assertIsNotNone(cmd)
        self.assertIn(cmd["action"], ("use", "move"))


class TestTreasureGating(unittest.TestCase):
    def _ctx(self):
        tp = TreasurePlanner()
        tp.apply_llm('{"x": 5, "y": 5, "items": ["StarSand"], "day": 4, "ready": true}')
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.treasure = tp
        return ctx

    def _turn(self, levels, with_task=False):
        tasks = ([{"pos": (14, 14), "text": "t", "scoreReward": 50,
                   "goldReward": 30, "timeoutRounds": 60}] if with_task else None)
        sim = make_sim(tasks=tasks)
        for i, lv in enumerate(levels):
            sim.roles.append(sim._role(50040 + i, 8 + i, 20, "rocket", 2000, level=lv))
        return Turn.load(sim.payload())

    def _pioneer(self, turn):
        return next(u for u in turn.ours if u.kind == "pioneer")

    def test_blocked_until_all_turrets_l3(self):
        turn = self._turn([1, 2, 3])
        self.assertFalse(PioneerFSM()._treasure_ready(turn, self._pioneer(turn), self._ctx()))

    def test_blocked_when_task_available(self):
        turn = self._turn([3, 3, 3], with_task=True)
        self.assertFalse(PioneerFSM()._treasure_ready(turn, self._pioneer(turn), self._ctx()),
                         "有任务可接时宝藏不抢占（低优先级）")

    def test_allowed_when_all_l3_and_no_task(self):
        turn = self._turn([3, 3, 3])
        self.assertTrue(PioneerFSM()._treasure_ready(turn, self._pioneer(turn), self._ctx()))


if __name__ == "__main__":
    unittest.main()
