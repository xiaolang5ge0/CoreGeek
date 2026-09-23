"""IKHZM0/IKHZKV/IKHZNN/IKHZOS/IKHZN7 回归：
- 中间墙夜间被打掉 → 次日白天必须补建（IKHZOS）
- 围墙修复包(WallFixer)优先级高于围墙升级券/升级任务（IKHZM0）
- 矿工夜间不再"原地待命"（D6 规则移除，决策 D）
- 规避粘性（防 evade↔采矿 两格震荡）
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
config.HARDCODED_ASSIST = False

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_worker import WorkerFSM, EVADE_STICKY
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn

DAY1 = 70


def run_rounds(brain, sim, n):
    for _ in range(n):
        r, _ = brain.decide(sim.payload())
        sim.apply(r)
        sim.advance()


class TestGapRebuild(unittest.TestCase):
    def test_gap_rebuilt_next_day(self):
        """IKHZOS：中间墙夜间被攻破 → 次日白天修理工必须去采石并补建。"""
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.add_mine((6, 22), "stone", remaining=300)
        sim.add_mine((8, 20), "copper", remaining=300)
        brain = Brain()
        run_rounds(brain, sim, DAY1)          # Day1 建墙
        self.assertGreaterEqual(len(sim.walls()), 8)
        # 模拟夜间被攻破一面墙（从布局中移除）
        victim = sim.walls()[0]
        sim.roles.remove(victim)
        n_before = len(sim.walls())
        # 修理工背包清空石料（幸存矿工转职场景：无石料）
        for w in [r for r in sim.roles if r["roleType"] == "worker"]:
            w["backpack"] = [x for x in w["backpack"] if x != "stone"]
        run_rounds(brain, sim, 130)           # Night1 + Day2
        self.assertGreater(
            len(sim.walls()), n_before, "中间墙缺口应在次日白天补建"
        )


class TestWallFixerPriority(unittest.TestCase):
    def test_wallfixer_stock_before_upgrade(self):
        """IKHZM0：D3+ 必须先备围墙修复包(WallFixer)，再买围墙升级券/做升级任务。"""
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.round_no = 261  # Day3
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(sim._role(90000 + i, pos[0], pos[1], "rocket", 1500, level=2))
        sim.roles.append(sim._role(91000, 12, 20, "wall", 1000, level=1))
        sim.gold = 300
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        fixer = [m for m in missions if m.voucher == "WallFixer"]
        self.assertTrue(fixer, "应备围墙修复包")
        self.assertGreaterEqual(fixer[0].qty, 3)
        wall_up = [m for m in missions if m.kind == "wall"]
        self.assertTrue(wall_up, "应有围墙升级任务")
        self.assertLess(
            fixer[0].priority, min(m.priority for m in wall_up),
            "修复包优先级必须高于围墙升级任务/券",
        )


class TestMinerNightActive(unittest.TestCase):
    def test_miner_not_idle_when_base_threatened(self):
        """决策 D：矿工夜间不再因"基地受威胁+在基地旁"而原地待命，应继续采矿。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        sim.round_no = 71 + 10          # Night1
        turn0 = Turn.load(sim.payload())
        w0 = next(u for u in turn0.ours if u.kind == "worker")
        sim.spawn_robot(w0.pos.x + 5, w0.pos.y, kind="smallRobot", hp=40)
        turn = Turn.load(sim.payload())
        worker = next(u for u in turn.ours if u.kind == "worker")
        fsm = WorkerFSM(worker.unit_id)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        ctx.robot_cells = frozenset([Pos(w0.pos.x + 5, w0.pos.y)])
        cmd = fsm._miner(turn, worker, ctx)
        self.assertIsNotNone(cmd, "矿工夜间应继续采矿，而非原地待命")


class TestEvadeSticky(unittest.TestCase):
    def test_evade_sticky_window(self):
        """规避粘性：进入危险距离后持续规避 EVADE_STICKY 回合（防两格震荡）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        sim.round_no = 71 + 10
        turn0 = Turn.load(sim.payload())
        w0 = next(u for u in turn0.ours if u.kind == "worker")
        sim.spawn_robot(w0.pos.x + 2, w0.pos.y, kind="smallRobot", hp=40)
        turn = Turn.load(sim.payload())
        worker = next(u for u in turn.ours if u.kind == "worker")
        fsm = WorkerFSM(worker.unit_id)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        ctx.robot_cells = frozenset([Pos(w0.pos.x + 2, w0.pos.y)])
        fsm._evade_cmd(turn, worker, ctx)
        self.assertTrue(fsm._evading)
        self.assertEqual(fsm._evade_until, turn.round_no + EVADE_STICKY)


class TestUpgradeOrder(unittest.TestCase):
    """用户 2026-09-23 升级顺序：L2炮台 > 受损墙 > L2围墙 > L3炮台 > L3围墙。"""

    def _sim(self, gold, weapons_level=2, wall_level=1):
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.round_no = 261  # Day3
        for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
            sim.roles.append(
                sim._role(90000 + i, pos[0], pos[1], "rocket", 1500, level=weapons_level)
            )
        # 目标 L2 的墙（front='W' → dx=2 中间），避免触发"正面墙最高优先"硬约束
        for i, pos in enumerate([(12, 22), (12, 23), (12, 24), (12, 25)]):
            sim.roles.append(
                sim._role(91000 + i, pos[0], pos[1], "wall", 1000, level=wall_level)
            )
        sim.gold = gold
        return sim

    def test_order_l2_weapon_before_l2_wall(self):
        sim = self._sim(700, weapons_level=1, wall_level=1)
        turn = Turn.load(sim.payload())
        ms = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        w1 = [m for m in ms if m.kind == "weapon" and m.voucher == "WeaponUpgradeVoucher1"]
        wall = [m for m in ms if m.kind == "wall"]
        self.assertTrue(w1, "应有 L2 炮台任务")
        self.assertTrue(wall)
        self.assertLess(
            min(m.priority for m in w1), min(m.priority for m in wall),
            "L2 炮台应优先于围墙升级",
        )

    def test_l2_wall_before_l3_weapon(self):
        sim = self._sim(900, weapons_level=2, wall_level=1)
        turn = Turn.load(sim.payload())
        ms = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        wall = [m for m in ms if m.kind == "wall"]
        w2 = [m for m in ms if m.kind == "weapon" and m.voucher == "WeaponUpgradeVoucher2"]
        self.assertTrue(wall, "应有 L2 围墙任务")
        self.assertTrue(w2, "应有 L3 炮台任务")
        self.assertLess(
            min(m.priority for m in wall), min(m.priority for m in w2),
            "L2 围墙应优先于 L3 炮台",
        )

    def test_wall_target_level(self):
        """目标等级（修订）：**纯正面**（迎敌侧整列不含拐角）= L3；其余 = L2。"""
        from agent.planners.upgrade import wall_target_level
        anchor = (10, 23)
        self.assertEqual(wall_target_level(Pos(13, 23), anchor, "W"), 3)  # 纯正面（中间）
        self.assertEqual(wall_target_level(Pos(13, 22), anchor, "W"), 3)  # 纯正面
        self.assertEqual(wall_target_level(Pos(13, 21), anchor, "W"), 2)  # 拐角 → L2
        self.assertEqual(wall_target_level(Pos(13, 26), anchor, "W"), 2)  # 拐角 → L2
        self.assertEqual(wall_target_level(Pos(12, 21), anchor, "W"), 2)  # 顶行靠敌 → L2
        self.assertEqual(wall_target_level(Pos(9, 21), anchor, "W"), 2)   # 靠开口侧 → L2


class TestOnlyRocket(unittest.TestCase):
    def test_gatling_not_upgraded(self):
        """用户 2026-09-23：只升火箭，gatling/railgun 不产生升级任务。"""
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.round_no = 261
        sim.roles.append(sim._role(80001, 9, 20, "gatling", 1000, level=1))
        sim.roles.append(sim._role(80002, 10, 20, "railgun", 1000, level=1))
        sim.roles.append(sim._role(80003, 9, 21, "rocket", 1000, level=1))
        sim.gold = 500
        turn = Turn.load(sim.payload())
        ms = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        wep = [m for m in ms if m.kind == "weapon"]
        self.assertTrue(wep, "应有火箭升级任务")
        self.assertTrue(
            all(m.target == Pos(9, 21) for m in wep), "只应升火箭（不含 gatling/railgun）"
        )


class TestNightDamageRegistry(unittest.TestCase):
    def test_accumulates_and_resets_daily(self):
        """紧急墙判据：本夜累计掉血跨回合累加，换日清零。"""
        from agent.wall_registry import WallRegistry
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.roles.append(sim._role(70001, 12, 22, "wall", 1000, level=2))
        reg = WallRegistry()
        reg.sync(Turn.load(sim.payload()))
        sim.role(70001)["health"] = 400
        reg.sync(Turn.load(sim.payload()))
        self.assertEqual(reg.walls[Pos(12, 22)].night_damage, 600)
        # 换日 → 清零
        sim.round_no += 130
        reg.sync(Turn.load(sim.payload()))
        self.assertEqual(reg.walls[Pos(12, 22)].night_damage, 0)


class TestExcessStoneSell(unittest.TestCase):
    def test_repairer_sells_excess_stone(self):
        """修理工只保留 max(5, walls_left) 石头，多余可卖（防背包塞满没空间备 WallFixer，IKI1T4）。"""
        from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.round_no = 261
        sim.role(10010)["backpack"] = ["stone"] * 20
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == 10010)
        fsm = WorkerFSM(10010)
        fsm.role = ROLE_REPAIRER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        ctx.walls_left = 0
        self.assertGreater(fsm._sellable_value(turn, unit, ctx), 0, "20 石头应可卖 15")
        # 墙环未建完（walls_left=14）→ 保留 14，仍可卖 6
        ctx.walls_left = 14
        self.assertGreater(fsm._sellable_value(turn, unit, ctx), 0)


class TestMineDistancePreference(unittest.TestCase):
    def test_prefers_nearer_mine(self):
        """资源效率：同价矿优先近矿（距离权重加大，减少长途）。"""
        from agent.fsm_worker import WorkerFSM
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.add_mine((6, 22), "copper", remaining=200)   # 近
        sim.add_mine((9, 12), "copper", remaining=200)   # 远
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == 10010)
        fsm = WorkerFSM(10010)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = False
        m = fsm._select_mine(turn, unit, ctx, prefer="money")
        self.assertEqual(m, Pos(6, 22), "应选近矿 (6,22)")


if __name__ == "__main__":
    unittest.main()
