"""D1–D8 决策落地回归（2026-09-22 用户拍板）。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.fsm_pioneer import DUSK_MARGIN
from agent.fsm_worker import REPAIR_MARGIN, RETURN_STICKY_DAY
from agent.planners.task import (
    FORCE_SUBMIT_CMDS, ST_LLM, ST_WAIT_CMD, TaskPlanner, TaskSession,
)
from agent.planners.upgrade import (
    FIXER_STOCK_MAX, FIXER_STOCK_MAXED, UpgradePlanner, wall_hp_threshold,
)
from agent.protocol import Pos, Turn

PIONEER = 10011
DAY1 = 70


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


def add_wall(sim, pos, level=1, health=1000, rid=None):
    rid = rid if rid is not None else 60000 + len(sim.walls())
    sim.roles.append(sim._role(rid, pos[0], pos[1], "wall", health, level=level))


def add_weapon(sim, pos, level=1, rid=None):
    rid = rid if rid is not None else 61000 + len(sim.weapons())
    sim.roles.append(sim._role(rid, pos[0], pos[1], "rocket", 1000, level=level))


class TestD2Margins(unittest.TestCase):
    def test_margins(self):
        self.assertEqual(DUSK_MARGIN, 5, "炮手归位余量=5")
        self.assertEqual(REPAIR_MARGIN, 3, "修理工归位余量=3")
        self.assertEqual(RETURN_STICKY_DAY, 3, "修理工 D3+ 回防粘性")


class TestD3WallJumpQueue(unittest.TestCase):
    def test_low_hp_wall_jumps_weapon_queue(self):
        """D3：墙血低于动态阈值 → 插队（优先于武器升级）。"""
        sim = make_sim(gold=300)
        add_weapon(sim, (9, 20), level=1)          # 武器待升 L2
        add_wall(sim, (12, 20), level=1, health=100)  # 远低于阈值
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        wall_m = [m for m in missions if m.kind == "wall"]
        self.assertTrue(wall_m, "低血墙应产生升级任务")
        self.assertLess(wall_m[0].priority, 10, "墙插队优先级应高于武器(10)")
        self.assertEqual(wall_hp_threshold(2), 300)

    def test_healthy_wall_does_not_jump(self):
        sim = make_sim(gold=300)
        add_weapon(sim, (9, 20), level=1)
        add_wall(sim, (12, 20), level=1, health=1000)  # 满血
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        prio = {m.kind: m.priority for m in missions}
        self.assertIn("weapon", prio)
        self.assertTrue(prio["weapon"] <= 10)


class TestD4BaseGated(unittest.TestCase):
    def _sim(self):
        sim = make_sim(gold=400)
        sim.roles = [r for r in sim.roles if r["roleType"] != "station"]
        sim.roles.append(sim._role(50013, 10, 24, "station", 1500, level=1))
        add_weapon(sim, (9, 20), level=2)
        add_weapon(sim, (10, 20), level=2)
        add_weapon(sim, (9, 21), level=2)
        add_wall(sim, (12, 20), level=2, health=1500)
        return sim

    def test_no_station_upgrade_before_maxed(self):
        sim = self._sim()
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        self.assertFalse([m for m in missions if m.kind == "station"],
                         "武器/墙未全满不得升基地")

    def test_station_upgrade_after_all_maxed(self):
        sim = self._sim()
        for w in sim.weapons():
            w["level"] = 3
            w["health"] = 2000
        for w in sim.walls():
            w["level"] = 3
            w["health"] = 2000
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        self.assertTrue([m for m in missions if m.kind == "station"],
                        "武器+墙全 L3 后允许升基地")


class TestD7CommandBudget(unittest.TestCase):
    def test_cmd_rejected_after_budget(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.cmd_count = FORCE_SUBMIT_CMDS
        planner._on_llm_result(s, '{"cmd":"curl x","answer":"","isFinished":false}')
        self.assertEqual(s.stage, ST_LLM, "命令数达上限后拒绝新命令，要求直接给答案")

    def test_cmd_allowed_before_budget(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.cmd_count = FORCE_SUBMIT_CMDS - 1
        planner._on_llm_result(s, '{"cmd":"curl x","answer":"","isFinished":false}')
        self.assertEqual(s.stage, ST_WAIT_CMD)


class TestD8FixerStock(unittest.TestCase):
    def test_capped_when_not_maxed(self):
        sim = make_sim(gold=300)
        sim.round_no = 261  # Day3
        add_weapon(sim, (9, 20), level=2)
        add_weapon(sim, (10, 20), level=2)
        add_weapon(sim, (9, 21), level=2)
        add_wall(sim, (12, 20), level=2, health=1500)
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock)
        self.assertLessEqual(stock[0].qty, FIXER_STOCK_MAX)

    def test_uncapped_when_all_maxed(self):
        sim = make_sim(gold=300)
        sim.round_no = 261  # Day3
        for pos in [(9, 20), (10, 20), (9, 21)]:
            add_weapon(sim, pos, level=3)
        for pos in [(12, 20), (12, 21)]:
            add_wall(sim, pos, level=3)
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock)
        self.assertEqual(stock[0].qty, FIXER_STOCK_MAXED, "全升满后不设上限")


class TestD1RepairerNightHold(unittest.TestCase):
    def test_d3_night_holds_repair_post_until_robots_cleared(self):
        """D1：D3+ 夜修理工守 repair_post；机器人清空后才外出采矿。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        for _ in range(DAY1):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        sim.round_no = 331  # Day3 夜
        sim.spawn_robot(30, 20, "smallRobot", hp=40, rid=30900)  # 远处机器人（未清空）
        post = brain.layout.repair_post
        held = False
        for _ in range(30):
            response, trace = brain.decide(sim.payload())
            info = (trace.get("workers") or {}).get("10010") or {}
            pos = sim.role(10010)["pos"]
            if max(abs(pos["x"] - post.x), abs(pos["y"] - post.y)) <= 1:
                held = True
            sim.apply(response)
            sim.advance()
        self.assertTrue(held, "D3 夜修理工应就位 repair_post（守内圈）")


if __name__ == "__main__":
    unittest.main()
