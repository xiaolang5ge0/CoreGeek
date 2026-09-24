"""issue IKI9X2 / IKI9XF 回归（2026-09-24）。

1. 备货任务卡死（IKI9X2 根因）：买不起时必须清掉，否则死占修理工、阻断所有墙升级。
2. 紧急抢修含临界 L1 墙（IKI9XF：L1 墙被打到 15 血、手里有包却不修）。
3. 归位受阻不得空转（IKI9XF：修理工整夜 RETURN_HOME 无指令）。
4. 清完己方兵潮且基地安全 → 火箭轰对面家（用户 IKI9XF：学对手的打法）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx
from agent.fire import JointFirePlanner
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1 = 10010


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestStuckStockClears(unittest.TestCase):
    def _fsm(self, gold):
        sim = make_sim(gold=gold)
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        fsm.upgrade = ("WallFixer", "stock", 15)
        ctx = _Ctx({})
        ctx.reserved = set()
        return turn, unit, fsm, ctx

    def test_unaffordable_stock_clears_mission(self):
        turn, unit, fsm, ctx = self._fsm(gold=0)
        cmd = fsm._upgrade_cmd(turn, unit, ctx, allow_use=True, allow_buy=True)
        self.assertIsNone(cmd)
        self.assertIsNone(fsm.upgrade, "买不起时应清掉备货任务（否则死占修理工、阻断墙升级）")

    def test_affordable_stock_keeps_mission(self):
        turn, unit, fsm, ctx = self._fsm(gold=300)
        cmd = fsm._upgrade_cmd(turn, unit, ctx, allow_use=True, allow_buy=True)
        self.assertIsNotNone(cmd, "买得起时应去采购")
        self.assertIsNotNone(fsm.upgrade)


class TestCriticalL1Repair(unittest.TestCase):
    def _setup(self, level, hp, bag):
        sim = make_sim()
        sim.roles.append(sim._role(40100, 13, 23, "wall", hp, level=level))
        sim.role(W1)["backpack"] = list(bag)
        sim.round_no = 461   # D4 夜
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

    def test_l1_critical_wall_repaired_with_pack(self):
        turn, unit, fsm, ctx = self._setup(1, 150, ["WallFixer"])   # 15%
        self.assertIsNotNone(fsm._critical_repair(turn, unit, ctx),
                             "临界 L1 墙也应抢修（夜间无法重建，被破即缺口）")

    def test_l1_healthy_wall_not_repaired(self):
        turn, unit, fsm, ctx = self._setup(1, 900, ["WallFixer"])
        self.assertIsNone(fsm._critical_repair(turn, unit, ctx))


class TestRepairerNotIdle(unittest.TestCase):
    def test_falls_through_when_home_blocked(self):
        sim = make_sim()
        sim.round_no = 461   # D4 夜
        sim.spawn_robot(12, 20, "middleRobot", hp=60)
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
        ctx.robot_cells = (Pos(12, 20),)
        ctx.safe_anchor = Pos(10, 24)
        fsm._go_home = lambda *a, **k: None          # 模拟归位受阻
        fsm._home_block_runs = 10                    # 已连续受阻 10 回合 → 本轮应退化
        called = {}
        fsm._miner = lambda *a, **k: called.setdefault("miner", True)
        fsm._repairer(turn, unit, ctx)
        self.assertTrue(called.get("miner"), "连续归位受阻 >10 回合应退化为采矿流程，不能整夜空转")


class TestEnemyBombardment(unittest.TestCase):
    def _plan(self, robots=()):
        sim = make_sim()
        sim.roles.append(sim._role(50040, 22, 10, "rocket", 2000, level=3))
        sim.role(10011)["pos"] = {"x": 21, "y": 10}   # 开拓者贴近火箭（操控需相邻）
        for i, (x, y) in enumerate(robots):
            sim.spawn_robot(x, y, "middleRobot", hp=60, rid=30900 + i)
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        commands, trace = {}, {}
        JointFirePlanner().plan(turn, pioneer, commands, trace)
        return commands, trace

    def test_fires_at_enemy_base_when_safe(self):
        commands, trace = self._plan()
        self.assertEqual(trace.get("fire", {}).get("mode"), "enemy_base",
                         "无兵潮且基地安全时应轰对面家")
        self.assertTrue(commands, "应向敌方建筑开火")

    def test_no_enemy_fire_when_base_threatened(self):
        commands, trace = self._plan(robots=[(11, 24)])
        self.assertNotEqual(trace.get("fire", {}).get("mode"), "enemy_base",
                            "基地附近有来犯机器人时不得分心打对面家")


if __name__ == "__main__":
    unittest.main()
