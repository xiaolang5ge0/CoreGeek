"""2026-09-22 用户 6 点策略落地回归。

1. 炮手任务间隙插空升级（TASK_TRAVEL 中也升级）
2. 修理工 D1 石料、D2+ 采矿；常备 5 石头
3. 修理工 D4+ 回防粘性（跨昼夜持续回墙内）
4. 宝藏（长上下文类）框架：累积/推断/行动
5. LLM 循环上限按 timeoutRounds 收紧
6. 推理类（官方消息）非任务期 LLM 兜底
"""
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_pioneer import (
    PioneerFSM,
    STATE_TASK_TRAVEL,
    STATE_WEAPON_UPGRADE,
)
from agent.fsm_worker import ROLE_REPAIRER, WorkerFSM
from agent.planners.news import NewsEconomy
from agent.planners.task import MAX_LLM_LOOPS, TaskPlanner, TaskSession
from agent.planners.treasure import TreasurePlanner
from agent.protocol import Pos, Turn

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


def run_rounds(brain, sim, n):
    for _ in range(n):
        response, _ = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()


class TestPioneerInterleaveUpgrade(unittest.TestCase):
    def test_upgrade_allowed_during_task_travel(self):
        """任务间隙插空升级：TASK_TRAVEL 途中仍可用券升级武器。"""
        sim = make_sim()
        sim.roles.append(sim._role(50040, 8, 20, "rocket", 1000, level=1))
        sim.role(PIONEER)["pos"] = {"x": 20, "y": 28}
        sim.role(PIONEER)["backpack"] = ["WeaponUpgradeVoucher1"]
        sim.round_no = 20  # 远离天黑
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        fsm = PioneerFSM()
        fsm.state = STATE_TASK_TRAVEL
        fsm.task_point = Pos(14, 14)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.gunner_upgrade = (Pos(8, 20), "weapon")
        cmd = fsm._day_cmd(turn, pioneer, Pos(9, 23), ctx)
        self.assertIsNotNone(cmd)
        self.assertEqual(fsm.state, STATE_WEAPON_UPGRADE, "应在任务途中插空升级")


class TestRepairerDayPlan(unittest.TestCase):
    def test_d2_prefers_money_mine(self):
        """D2 修理工应采铜/铁（money），而非石矿。"""
        sim = make_sim(mines={(6, 22): "stone", (12, 20): "copper"})
        sim.add_mine((6, 22), "stone", remaining=50)
        sim.add_mine((12, 20), "copper", remaining=50)
        sim.round_no = 140  # Day2
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.walls_left = 0
        ctx.dusk_avoid = False
        cmd = fsm._repairer(turn, unit, ctx)
        self.assertIsNotNone(cmd)
        self.assertEqual(turn.zones.get(fsm.mine), "copper", "D2 应优先铜矿")

    def test_repairer_keeps_5_stone(self):
        """修理工常备 5 石头：卖石时保留 5。"""
        sim = make_sim()
        sim.role(W1)["backpack"] = ["stone"] * 10
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        ctx = _Ctx({})
        ctx.walls_left = 0
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        cmd = fsm._sell_best(turn, unit, ctx)
        self.assertEqual(cmd["num"], 5, "应保留 5 石头")
        # 挖矿工则全卖
        fsm.role = "miner"
        cmd = fsm._sell_best(turn, unit, ctx)
        self.assertEqual(cmd["num"], 10)


class TestRepairerStickyReturn(unittest.TestCase):
    def test_returns_through_night_d4(self):
        """D4+ 回防粘性：入夜后仍持续 RETURN_HOME（不切去采矿/守夜）。"""
        sim = make_sim(mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        sim.role(W1)["pos"] = {"x": 22, "y": 30}
        sim.gold = 0
        sim.round_no = 457  # Day4 白天后段（距天黑 4 回合）
        night_states = []
        for _ in range(20):
            response, trace = brain.decide(sim.payload())
            if not sim._is_day():
                info = (trace.get("workers") or {}).get(str(W1)) or {}
                night_states.append(info.get("state"))
            sim.apply(response)
            sim.advance()
        self.assertIn("RETURN_HOME", night_states, "D4+ 入夜后应继续回防")


class TestLoopLimitByTimeout(unittest.TestCase):
    def test_max_loops_from_timeout(self):
        """LLM 循环上限按任务 timeoutRounds 收紧（timeout-2，且不超过默认值）。"""
        task = {"pos": (14, 14), "text": "请阅读task_1_beijing.md，获取任务信息",
                "scoreReward": 50, "goldReward": 30, "timeoutRounds": 6}
        sim = make_sim(tasks=[task])
        sim.role(PIONEER)["pos"] = {"x": 14, "y": 14}
        sim.phase_task = task["text"]
        turn = Turn.load(sim.payload())
        session = TaskSession()
        TaskPlanner().work(turn, session)
        self.assertEqual(session.max_loops, 4)  # min(8, 6-2)
        self.assertLess(session.max_loops, MAX_LLM_LOOPS)


class TestNewsLlmFallback(unittest.TestCase):
    def test_deterministic_fail_triggers_llm(self):
        """无矿种/停工关键词的官方消息 → 触发非任务期 LLM 兜底并生效。"""
        ne = NewsEconomy()
        ne.update("边境传来难以名状的低鸣，侦察队去向不明", 1)
        self.assertTrue(ne.last_new)
        self.assertFalse(ne.last_parsed)
        self.assertTrue(ne.apply_llm('{"ore":"iron","action":"stop","start_day":2,"days":2}'))
        self.assertGreater(ne.boost("iron", 2), 0)

    def test_brain_queues_and_applies(self):
        base = json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
        base["worldNews"] = {"officialNews": "边境传来难以名状的低鸣", "folkLegends": ""}
        base["llmResp"] = ""
        brain = Brain()
        _, trace = brain.decide(base)
        self.assertEqual(brain._llm_waiting, "news")
        self.assertTrue(trace.get("news_llm"))
        self.assertEqual(brain.llm_calls_today, 1)
        base["roundNo"] = 2
        base["llmResp"] = '{"ore":"iron","action":"stop","start_day":2,"days":2}'
        brain.decide(base)
        self.assertIn("iron", brain.news_economy.predictions)
        self.assertEqual(brain._llm_waiting, "")


class TestTreasureFramework(unittest.TestCase):
    def test_observe_and_infer(self):
        tp = TreasurePlanner()
        self.assertTrue(tp.observe("传闻一：石门在东方"))
        self.assertFalse(tp.observe("传闻一：石门在东方"))
        self.assertTrue(tp.needs_inference())
        self.assertTrue(tp.apply_llm(
            '{"x": 5, "y": 5, "items": ["StarSand"], "day": 3, "ready": true}'
        ))
        self.assertTrue(tp.plan.ready)
        self.assertEqual(tp.plan.location, Pos(5, 5))

    def test_cmd_buys_then_summons(self):
        sim = make_sim(gold=100, shop=(25, 20))
        tp = TreasurePlanner()
        tp.apply_llm('{"x": 12, "y": 20, "items": ["StarSand"], "day": 3, "ready": true}')
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        ctx = _Ctx({})
        ctx.reserved = set()
        # 祭品未备 → 去商店（move 或买）
        cmd = tp.cmd(turn, pioneer, Pos(25, 20), ctx)
        self.assertIsNotNone(cmd)
        self.assertIn(cmd["action"], ("move", "buy"))
        # 备齐祭品且在祭坛旁 → summonTreasure
        sim.role(PIONEER)["backpack"] = ["StarSand"]
        sim.role(PIONEER)["pos"] = {"x": 11, "y": 20}
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        cmd = tp.cmd(turn, pioneer, Pos(25, 20), ctx)
        self.assertEqual(cmd["action"], "summonTreasure")
        self.assertEqual(cmd["item"], ["StarSand"])

    def test_not_ready_no_action(self):
        sim = make_sim(gold=100)
        tp = TreasurePlanner()
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        ctx = _Ctx({})
        ctx.reserved = set()
        self.assertIsNone(tp.cmd(turn, pioneer, Pos(25, 20), ctx))


if __name__ == "__main__":
    unittest.main()