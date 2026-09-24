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
        # 新策略（用户 2026-09-23）：L2炮台(10) > 受损墙(12) > L2围墙(15) > ...
        self.assertLess(wall_m[0].priority, 15, "受损墙应优先于一般墙升级(15)")
        self.assertGreater(wall_m[0].priority, 10, "L2炮台(10)优先于受损墙")
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
        sim.round_no = 521  # Day5（D1–D4 有 15 上限；D5 起恢复满备，用户 2026-09-24）
        for pos in [(9, 20), (10, 20), (9, 21)]:
            add_weapon(sim, pos, level=3)
        for pos in [(12, 20), (12, 21)]:
            add_wall(sim, pos, level=3)
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock)
        self.assertEqual(stock[0].qty, FIXER_STOCK_MAXED, "D5+ 全升满后不设上限")

    def test_early_cap_15_before_d5(self):
        """D1–D4 修复包上限 15（用户 2026-09-24：D3 囤 30 太多）。"""
        from agent.planners.upgrade import FIXER_STOCK_EARLY
        sim = make_sim(gold=300)
        sim.round_no = 261  # Day3
        for pos in [(9, 20), (10, 20), (9, 21)]:
            add_weapon(sim, pos, level=3)
        for pos in [(12, 20), (12, 21)]:
            add_wall(sim, pos, level=3)
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        stock = [m for m in missions if m.kind == "stock" and m.voucher == "WallFixer"]
        self.assertTrue(stock)
        self.assertEqual(stock[0].qty, FIXER_STOCK_EARLY, "D3 修复包上限应为 15")


class TestD1RepairerNightHold(unittest.TestCase):
    def test_d3_night_holds_repair_post_until_robots_cleared(self):
        """D1：D3+ 夜机器人在场时修理工回防就位（不外出采矿）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        for _ in range(DAY1):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        sim.round_no = 331  # Day3 夜
        post = brain.layout.repair_post
        reached = False
        for _ in range(40):
            # 保持有机器人（远处、高血量）→ 修理工应持续回防
            sim.robots.clear()
            sim.spawn_robot(30, 20, "smallRobot", hp=400, rid=30900)
            response, trace = brain.decide(sim.payload())
            info = (trace.get("workers") or {}).get("10010") or {}
            pos = sim.role(10010)["pos"]
            if max(abs(pos["x"] - post.x), abs(pos["y"] - post.y)) <= 1 \
                    or info.get("state") == "RETURN_HOME":
                reached = True
            sim.apply(response)
            sim.advance()
        self.assertTrue(reached, "D3 夜机器人在场时修理工应回防就位（不外出采矿）")


class TestWallUpgradeOrder(unittest.TestCase):
    def test_wall_rank_classification(self):
        """任务2：正面 → 拐角 → 侧面 分级（front=开口侧，敌人=反向）。"""
        from agent.planners.upgrade import WALL_CORNER, WALL_FRONT, WALL_SIDE, wall_rank
        # front=W（开口西，敌人东）：dx=3 为正面；dx=3 且 dy=±端点 为拐角；其余侧面
        self.assertEqual(wall_rank(Pos(3, 0), (0, 0), "W"), WALL_FRONT)
        self.assertEqual(wall_rank(Pos(3, -2), (0, 0), "W"), WALL_CORNER)
        self.assertEqual(wall_rank(Pos(2, 0), (0, 0), "W"), WALL_SIDE)
        # front=E：dx=-2 为正面
        self.assertEqual(wall_rank(Pos(-2, 0), (0, 0), "E"), WALL_FRONT)
        self.assertEqual(wall_rank(Pos(-2, 3), (0, 0), "E"), WALL_CORNER)

    def test_upgrade_order_front_corner_side(self):
        """墙 L1→L2 任务顺序：正面 → 拐角 → 侧面。"""
        from agent.planners.upgrade import UpgradePlanner, wall_rank
        sim = make_sim(gold=400)
        for pos in [(9, 20), (10, 20), (9, 21)]:
            add_weapon(sim, pos, level=2)
        brain = Brain()
        brain.decide(sim.payload())
        front = brain.front
        for cell in brain.layout.wall_cells:
            add_wall(sim, (cell.x, cell.y), level=1, health=1000)
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=brain.layout.control_point, front=front)
        st = sim.role(10013)["pos"]
        anchor = (st["x"], st["y"] - 1)
        wall_order = [
            wall_rank(m.target, anchor, front)
            for m in missions if m.kind == "wall" and m.target is not None
        ]
        self.assertTrue(wall_order)
        self.assertEqual(wall_order, sorted(wall_order), "墙升级应 正面→拐角→侧面（rank 递增）")


if __name__ == "__main__":
    unittest.main()
