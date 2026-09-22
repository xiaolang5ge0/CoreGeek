"""围墙状态表 + L3 修复策略回归（用户补充说明 2026-09-22）。

覆盖：
1. WallRegistry 跨回合识别"被攻破 / 补建"（exists/level/health Dict）。
2. 修复阈值 <50%；补建墙重新进入升级队列（upgradable 排序）。
3. 夜间抢修：L1/L2 优先升级券（升级=回满血），L3 只能用 WallFixer。
4. 升级器按需备货 WallFixer（Day4+ / 出现 L3 墙时）。
"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_worker import ROLE_REPAIRER, WorkerFSM
from agent.planners.layout import choose_front, compute_layout
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn
from agent.wall_registry import REPAIR_HP_RATIO, WallRegistry

W1 = 10010
DAY1 = 70


def make_sim():
    return SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})


def add_wall(sim, cell, health=1000, level=1, rid=None):
    rid = rid if rid is not None else 40000 + len(sim.walls())
    sim.roles.append(sim._role(rid, cell.x, cell.y, "wall", health, level=level))
    return rid


def free_neighbour(turn, cell):
    occupied = turn.occupied_cells()
    for nb in cell.neighbours():
        if turn.land(nb) and nb not in occupied:
            return nb
    return None


class TestWallRegistryTracking(unittest.TestCase):
    def test_breach_and_rebuild_events(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())  # 建立布局 + 初始化状态表
        reg = brain.wall_registry
        self.assertIsInstance(reg, WallRegistry)
        self.assertGreater(len(reg.missing()), 0, "未建造时布局墙应记为缺口")

        cell = brain.layout.wall_cells[0]
        rid = add_wall(sim, cell, health=1000, level=1)
        brain.decide(sim.payload())
        state = reg.walls[cell]
        self.assertTrue(state.exists)
        self.assertEqual(state.level, 1)
        self.assertEqual(state.health, 1000)

        # 夜战被攻破
        sim.roles = [r for r in sim.roles if r["id"] != rid]
        brain.decide(sim.payload())
        self.assertFalse(reg.walls[cell].exists)
        self.assertGreater(reg.walls[cell].breached_round, 0)
        self.assertIn(reg.walls[cell], reg.breached())

        # 次日补建（回到 L1）
        add_wall(sim, cell, health=1000, level=1)
        brain.decide(sim.payload())
        state = reg.walls[cell]
        self.assertTrue(state.exists)
        self.assertEqual(state.level, 1)
        self.assertGreater(state.rebuilt_round, 0)
        self.assertIn(state, reg.recently_rebuilt())

    def test_damaged_threshold_50pct(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        cells = brain.layout.wall_cells
        add_wall(sim, cells[0], health=600, level=1)  # 60% → 未达修复阈值
        add_wall(sim, cells[1], health=400, level=1)  # 40% → 受损
        brain.decide(sim.payload())
        reg = brain.wall_registry
        damaged_pos = {s.pos for s in reg.damaged()}
        self.assertNotIn(cells[0], damaged_pos)
        self.assertIn(cells[1], damaged_pos)
        self.assertEqual(REPAIR_HP_RATIO, 0.5)

    def test_rebuilt_wall_prioritized_for_upgrade(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        reg = brain.wall_registry
        cells = brain.layout.wall_cells
        # 模拟：一面墙前夜被攻破、今日补建（L1，满血）；其余为普通健康 L1 墙
        rebuilt = cells[0]
        reg.walls[rebuilt].exists = True
        reg.walls[rebuilt].level = 1
        reg.walls[rebuilt].health = 1000
        reg.walls[rebuilt].rebuilt_round = 999
        reg.walls[cells[1]].exists = True
        reg.walls[cells[1]].level = 1
        reg.walls[cells[1]].health = 1000
        order = reg.upgradable()
        self.assertEqual(order[0].pos, rebuilt, "补建墙应优先重新升级")


class TestNightRepairPolicy(unittest.TestCase):
    def _repair_cmd(self, sim, brain, cell, level, health, backpack):
        sim.role(W1)["backpack"] = list(backpack)
        rid = add_wall(sim, cell, health=health, level=level)
        turn = Turn.load(sim.payload())
        nb = free_neighbour(turn, cell)
        self.assertIsNotNone(nb, "需要墙旁空地进行测试")
        sim.role(W1)["pos"] = {"x": nb.x, "y": nb.y}
        turn = Turn.load(sim.payload())
        brain.wall_registry.sync(turn, brain.layout)
        ctx = _Ctx({})
        ctx.wall_registry = brain.wall_registry
        ctx.reserved = set()
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        unit = next(u for u in turn.ours if u.unit_id == W1)
        return fsm._repair_cmd(turn, unit, ctx), rid

    def test_low_level_uses_upgrade_voucher(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        cell = brain.layout.wall_cells[0]
        cmd, _ = self._repair_cmd(sim, brain, cell, 1, 400, ["WallUpgradeVoucher1"])
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "use")
        self.assertEqual(cmd["name"], "WallUpgradeVoucher1")

    def test_max_level_uses_fixer(self):
        """L3 墙升级券失效 → 只能 WallFixer。"""
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        cell = brain.layout.wall_cells[0]
        cmd, _ = self._repair_cmd(
            sim, brain, cell, 3, 400, ["WallUpgradeVoucher2", "WallFixer"]
        )
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["name"], "WallFixer")

    def test_above_threshold_no_repair(self):
        """血量 >50% 不修（不浪费修复包）。"""
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        cell = brain.layout.wall_cells[0]
        cmd, _ = self._repair_cmd(sim, brain, cell, 1, 700, ["WallUpgradeVoucher1", "WallFixer"])
        self.assertIsNone(cmd)


class TestFixerStockOnDemand(unittest.TestCase):
    def _plan(self, sim, brain, round_no):
        sim.round_no = round_no
        turn = Turn.load(sim.payload())
        reg = brain.wall_registry
        reg.sync(turn, brain.layout)
        return UpgradePlanner().plan(turn, cp=brain.layout.control_point, registry=reg)

    def test_stock_from_day3(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        sim.gold = 300
        missions = self._plan(sim, brain, 261)  # Day3
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock, "Day3+ 应按需备货 WallFixer")
        self.assertGreaterEqual(stock[0].qty, 1)

    def test_stock_scales_with_l3_walls_day4(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        cells = brain.layout.wall_cells
        for i in range(3):
            add_wall(sim, cells[i], health=2000, level=3, rid=41000 + i)
        sim.gold = 300
        missions = self._plan(sim, brain, 391)  # Day4（BOSS 夜）
        stock = [m for m in missions if m.kind == "stock"]
        self.assertTrue(stock)
        self.assertEqual(stock[0].qty, 3, "3 面 L3 墙 → 备 3 个修复包")

    def test_no_stock_when_already_held(self):
        sim = make_sim()
        brain = Brain()
        brain.decide(sim.payload())
        sim.role(W1)["backpack"] = ["WallFixer", "WallFixer"]  # 常备 2 个 → 不再备货
        sim.gold = 300
        missions = self._plan(sim, brain, 261)
        self.assertFalse([m for m in missions if m.kind == "stock"])


class TestNightRepairIntegration(unittest.TestCase):
    def test_day4_l3_wall_repaired_with_fixer(self):
        """端到端：Day4 夜，L3 受损墙 → 修理工用 WallFixer 修复（升级券已失效）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=80)
        sim.add_mine((7, 26), "stone", remaining=80)
        brain = Brain()
        for _ in range(DAY1):
            response, _ = brain.decide(sim.payload())
            sim.apply(response)
            sim.advance()
        self.assertGreaterEqual(len(sim.walls()), 10)
        for wall in sim.walls()[:3]:
            wall["level"] = 3
            wall["health"] = 400  # 20% < 50%
        sim.role(W1)["backpack"] = ["WallFixer"]
        sim.round_no = 461  # Day4 夜首回合
        used = False
        for _ in range(40):
            response, _ = brain.decide(sim.payload())
            for cmd in response["roleCommandMap"].values():
                if cmd.get("action") == "use" and cmd.get("name") == "WallFixer":
                    used = True
            sim.apply(response)
            sim.advance()
        self.assertTrue(used, "Day4 夜应使用 WallFixer 修复 L3 受损墙")


if __name__ == "__main__":
    unittest.main()
