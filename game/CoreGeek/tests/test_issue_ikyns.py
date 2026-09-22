"""IKHYNS 异常红线 + 围墙券2/修复券 回归。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.planners.upgrade import UpgradePlanner, FIXER_STOCK_MAX
from agent.protocol import Pos, Turn
from agent.threat import ThreatEstimator


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestThreatEmptyDefense(unittest.TestCase):
    def test_no_crash_when_all_buildings_gone(self):
        """IKHYNS 红线：基地+墙+武器全毁但仍有机器人时，threat 不得崩溃。"""
        sim = make_sim()
        # 删掉基地/武器/墙，只留工人+机器人
        sim.roles = [r for r in sim.roles if r["roleType"] in ("worker", "pioneer")]
        sim.spawn_robot(12, 22, "largeRobot", hp=500, rid=70001)
        turn = Turn.load(sim.payload())
        report = ThreatEstimator().evaluate(turn)   # 不得抛 ValueError
        self.assertIsNotNone(report)
        self.assertEqual(report.nearest_dist, 99)

    def test_decide_never_raises(self):
        """decide 顶层兜底：内部异常回落合法空响应（异常=封号红线）。"""
        sim = make_sim()
        sim.roles = [r for r in sim.roles if r["roleType"] in ("worker", "pioneer")]
        sim.spawn_robot(12, 22, "largeRobot", hp=500, rid=70002)
        brain = Brain()
        resp, trace = brain.decide(sim.payload())
        self.assertIn("roleCommandMap", resp)
        self.assertIsInstance(trace, dict)


class TestWallVoucher2Jump(unittest.TestCase):
    def test_l2_damaged_wall_gets_voucher2(self):
        """正面 L2 墙低于阈值 → Voucher2（升 L3 回血）。"""
        sim = make_sim(gold=200)
        sim.round_no = 261
        for pos in [(9, 20), (10, 20), (9, 21)]:
            sim.roles.append(sim._role(62000 + len(sim.weapons()), pos[0], pos[1], "rocket", 1500, level=2))
        # 一面 L2 受损墙（远低于阈值）
        sim.roles.append(sim._role(63000, 12, 20, "wall", 200, level=2))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        w2 = [m for m in missions if m.kind == "wall" and m.voucher == "WallUpgradeVoucher2"]
        self.assertTrue(w2, "L2 受损墙应产生 Voucher2 升级任务")
        self.assertLess(w2[0].priority, 10, "应插队于武器之前")


class TestD4FixerStock(unittest.TestCase):
    def test_d4_stocks_at_least_3(self):
        sim = make_sim(gold=300)
        sim.round_no = 391  # Day4
        for pos in [(9, 20), (10, 20), (9, 21)]:
            sim.roles.append(sim._role(64000 + len(sim.weapons()), pos[0], pos[1], "rocket", 1500, level=2))
        sim.roles.append(sim._role(65000, 12, 20, "wall", 1500, level=2))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock)
        self.assertGreaterEqual(stock[0].qty, 3)
        self.assertLessEqual(stock[0].qty, FIXER_STOCK_MAX)


if __name__ == "__main__":
    unittest.main()
