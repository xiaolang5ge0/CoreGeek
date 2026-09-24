"""issue IKI8KF / IKI8KL / IKI8KM / IKI8KN 回归（2026-09-24）。

1. 出兵点/走廊：D1 入夜前 3 回合"往最近地图边缘走"（首日不知出兵点）；黄昏走出走廊。
2. 宝藏：prompt 给出地图边界（防越界坐标被丢）；炮塔未全 L3 但买不起升级券时也允许去宝藏。
3. 任务 prompt：给出 oldest_era 年代序；禁止用 head -c 截断（漏记录 → 统计错）。
4. 围墙：D1–D4 修复包上限 15；升级券备货按墙等级、最多 5 张、优先级 13；D5 起侧墙不得停 L1。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.planners.task import TaskPlanner
from agent.planners.treasure import TreasurePlanner
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1 = 10010


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestD1EdgeAvoid(unittest.TestCase):
    def _setup(self, round_no):
        sim = make_sim()
        sim.round_no = round_no
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        ctx = _Ctx({})
        ctx.reserved = set()
        return turn, unit, fsm, ctx

    def test_last_three_rounds_move_to_edge(self):
        turn, unit, fsm, ctx = self._setup(69)   # D1，距入夜 2 回合
        cmd = fsm._d1_edge_avoid(turn, unit, ctx)
        self.assertIsNotNone(cmd, "D1 入夜前 3 回合应紧急避让")
        self.assertEqual(cmd["action"], "move")

    def test_not_early_in_day(self):
        turn, unit, fsm, ctx = self._setup(30)   # D1 白天中段
        self.assertIsNone(fsm._d1_edge_avoid(turn, unit, ctx))

    def test_not_on_later_days(self):
        turn, unit, fsm, ctx = self._setup(69 + 130)   # D2 同时刻
        self.assertIsNone(fsm._d1_edge_avoid(turn, unit, ctx))


class TestDuskCorridorEscape(unittest.TestCase):
    def test_worker_in_corridor_walks_out(self):
        sim = make_sim()
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = True
        ctx.corridor_cells = (unit.pos,)
        ctx.spawn_cells = ()
        ctx.safe_anchor = Pos(10, 22)
        cmd = fsm._dusk_corridor_escape(turn, unit, ctx)
        self.assertIsNotNone(cmd, "黄昏站在走廊里应先走出来")

    def test_no_escape_when_far(self):
        sim = make_sim()
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.dusk_avoid = True
        ctx.corridor_cells = (Pos(35, 2),)
        ctx.spawn_cells = ()
        ctx.safe_anchor = Pos(10, 24)
        self.assertIsNone(fsm._dusk_corridor_escape(turn, unit, ctx))


class TestTreasurePromptBounds(unittest.TestCase):
    def test_prompt_states_map_bounds(self):
        p = TreasurePlanner()
        p.observe("传闻：石门在东方")
        text = p.prompt(41, 32)
        self.assertIn("41×32", text)
        self.assertIn("[0,40]", text)
        self.assertIn("[0,31]", text)


class TestApiContract(unittest.TestCase):
    def test_contract_has_era_order_and_no_head(self):
        c = TaskPlanner._contract("api")
        self.assertIn("oldest_era", c)
        self.assertIn("旧石器", c)
        self.assertIn("明清", c)
        self.assertNotIn("head -c 4000", c, "不应建议截断（会漏记录）")
        self.assertIn("完整", c)


class TestSideWallsFromD5(unittest.TestCase):
    def _plan(self, round_no):
        sim = make_sim()
        sim.round_no = round_no
        sim.roles.append(sim._role(40200, 8, 21, "wall", 1000, level=1))   # 侧面 L1 墙
        turn = Turn.load(sim.payload())
        return UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")

    def test_d5_side_l1_prioritised(self):
        plan = self._plan(521)   # D5
        m = [x for x in plan if x.target == Pos(8, 21)]
        self.assertTrue(m, "D5 起侧面 L1 墙应进入升级队列")
        self.assertLessEqual(min(x.priority for x in m), 25)

    def test_d6_higher_priority(self):
        plan = self._plan(651)   # D6
        m = [x for x in plan if x.target == Pos(8, 21)]
        self.assertTrue(m)
        self.assertLessEqual(min(x.priority for x in m), 23, "D6 起优先级更高（不得停在 L1）")


class TestWallVoucherStock(unittest.TestCase):
    def test_stock_priority_and_qty(self):
        sim = make_sim()
        sim.round_no = 261   # D3
        for y in (22, 23, 24, 25):
            sim.roles.append(sim._role(40300 + y, 13, y, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        plan = UpgradePlanner().plan(turn, cp=Pos(10, 24), front="W")
        stock = [m for m in plan if m.kind == "stock" and m.voucher == "WallUpgradeVoucher1"]
        self.assertTrue(stock, "D3 应按墙等级备升级券（否则夜里没券可升）")
        self.assertEqual(stock[0].priority, 30)
        self.assertLessEqual(stock[0].qty, 5)


if __name__ == "__main__":
    unittest.main()
