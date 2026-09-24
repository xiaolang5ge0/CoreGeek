"""issue IKIB4O 回归（2026-09-24）：围墙策略调整。

用户要求：
1. D1–D4 修复包上限 8（原 15）；前期优先级 **武器升级 > 围墙升级 > 围墙修复**。
2. 围墙升级券备货优先级提高（按墙的当前等级 vs 目标差距买对应券）；
   **只有墙都升到目标等级了，才降级考虑买修复包**（D5+ 上限 30）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.upgrade import FIXER_STOCK_EARLY, UpgradePlanner
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestWallPriorityOrder(unittest.TestCase):
    def _plan(self, gold=1000, weapons=2, walls=1, day_round=261):
        sim = make_sim(gold=gold)
        sim.round_no = day_round
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(sim._role(80000 + i, pos[0], pos[1], "rocket", 1500, level=weapons))
        for i, pos in enumerate([(13, 22), (13, 23), (13, 24), (13, 25)]):   # 正面列
            sim.roles.append(sim._role(81000 + i, pos[0], pos[1], "wall", 1000, level=walls))
        turn = Turn.load(sim.payload())
        return UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")

    def test_weapon_before_wall_before_repair(self):
        """武器升级(5/10) > 围墙升级(15~28) > 围墙修复/备货(40)。"""
        plan = self._plan()
        weapon = [m for m in plan if m.kind == "weapon"]
        wall = [m for m in plan if m.kind == "wall"]
        fixer = [m for m in plan if m.kind == "stock" and m.voucher == "WallFixer"]
        self.assertTrue(weapon and wall and fixer)
        self.assertLess(min(m.priority for m in weapon), min(m.priority for m in wall),
                        "武器升级应优先于围墙升级")
        self.assertLess(max(m.priority for m in wall), fixer[0].priority,
                        "围墙升级应优先于修复包备货（升级>修复）")

    def test_front_wall_is_top_wall_priority(self):
        plan = self._plan()
        wall = [m for m in plan if m.kind == "wall"]
        self.assertEqual(min(m.priority for m in wall), 15, "正面墙应为墙类最高优先")


class TestFixerEarlyCap(unittest.TestCase):
    def test_early_cap_is_8(self):
        self.assertEqual(FIXER_STOCK_EARLY, 8)

    def test_d3_stock_qty_capped_at_8(self):
        sim = make_sim(gold=1000)
        sim.round_no = 261   # D3
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(sim._role(82000 + i, pos[0], pos[1], "rocket", 2000, level=3))
        turn = Turn.load(sim.payload())
        plan = UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")
        fixer = [m for m in plan if m.kind == "stock" and m.voucher == "WallFixer"]
        self.assertTrue(fixer)
        self.assertEqual(fixer[0].qty, 8, "D1–D4 修复包上限应为 8")

    def test_d5_stock_allows_30(self):
        sim = make_sim(gold=1000)
        sim.round_no = 521   # D5
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(sim._role(83000 + i, pos[0], pos[1], "rocket", 2000, level=3))
        turn = Turn.load(sim.payload())
        plan = UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")
        fixer = [m for m in plan if m.kind == "stock" and m.voucher == "WallFixer"]
        self.assertTrue(fixer)
        self.assertGreater(fixer[0].qty, 8, "D5+ 修复包上限放宽（升到目标后最多 30）")


if __name__ == "__main__":
    unittest.main()
