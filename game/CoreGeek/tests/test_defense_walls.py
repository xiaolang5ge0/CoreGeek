"""围墙防御死线回归（用户 2026-09-23）：D3 正面≥5 L2 / D5 正面≥5 L3 / 白天可升级 / 券备货≥5。"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
config.HARDCODED_ASSIST = True  # 本模块测策略规划，非任务硬编码

from harness import SimWorld
from agent.brain import Brain
from agent.fsm_worker import ROLE_REPAIRER, WorkerFSM
from agent.planners.upgrade import (
    FRONT_L2_TARGET, FRONT_L3_TARGET, UpgradePlanner, wall_rank,
)
from agent.protocol import Pos, Turn

W1 = 10010


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


def _front_cells(sim):
    """返回正面列（含拐角）的坐标：station (10,24) front='W' → x=13 整列。"""
    st = sim.role(10013)["pos"]
    anchor = (st["x"], st["y"] - 1)
    cells = []
    for y in range(st["y"] - 3, st["y"] + 4):
        p = Pos(st["x"] + 3, y)
        if wall_rank(p, anchor, "W") in (0, 1):
            cells.append(p)
    return cells, anchor


def _add_weapons_l2(sim):
    for i, pos in enumerate([(9, 20), (10, 20), (9, 21)]):
        sim.roles.append(sim._role(70000 + i, pos[0], pos[1], "rocket", 1500, level=2))


class TestFrontWallDeadlines(unittest.TestCase):
    def test_d3_front_l2_deadline(self):
        """D3 前正面 <5 面 L2 → 产生正面 L1→L2 任务（优先级 12，武器 L2 之后）。"""
        sim = make_sim(gold=400)
        sim.round_no = 261  # Day3
        _add_weapons_l2(sim)
        cells, anchor = _front_cells(sim)
        for i, p in enumerate(cells[:6]):
            sim.roles.append(sim._role(71000 + i, p.x, p.y, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        front_l2 = [m for m in missions
                    if m.kind == "wall" and m.voucher == "WallUpgradeVoucher1"
                    and wall_rank(m.target, anchor, "W") in (0, 1)]
        self.assertGreaterEqual(len(front_l2), 1, "应排正面墙 L2 任务")
        # 新策略：L2围墙优先级 15（L2炮台 10 > 受损墙 12 > L2围墙 15）
        self.assertEqual(min(m.priority for m in front_l2), 15)
        self.assertGreaterEqual(FRONT_L2_TARGET, 5)

    def test_d5_front_l3_deadline(self):
        """D5 前正面 <5 面 L3 → 正面 L2 墙产生 Voucher2 任务。"""
        sim = make_sim(gold=400)
        sim.round_no = 521  # Day5
        _add_weapons_l2(sim)
        cells, anchor = _front_cells(sim)
        for i, p in enumerate(cells[:6]):
            sim.roles.append(sim._role(72000 + i, p.x, p.y, "wall", 1500, level=2))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        front_l3 = [m for m in missions
                    if m.kind == "wall" and m.voucher == "WallUpgradeVoucher2"
                    and wall_rank(m.target, anchor, "W") in (0, 1)]
        self.assertGreaterEqual(len(front_l3), 1, "应排正面墙 L3（Voucher2）任务")
        # 新策略：L3围墙优先级 25（L2炮台10 > 受损墙12 > L2围墙15 > L3炮台20 > L3围墙25）
        self.assertEqual(min(m.priority for m in front_l3), 25)
        self.assertGreaterEqual(FRONT_L3_TARGET, 5)

    def test_weapon_l2_first_gate(self):
        """武器未到 L2 时，不得为墙券/墙死线花钱（金币充足时武器优先）。"""
        sim = make_sim(gold=400)
        sim.round_no = 261
        sim.roles.append(sim._role(73000, 9, 20, "rocket", 1000, level=1))  # 武器 L1
        cells, anchor = _front_cells(sim)
        for i, p in enumerate(cells[:6]):
            sim.roles.append(sim._role(73100 + i, p.x, p.y, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        self.assertFalse([m for m in missions
                          if m.kind == "stock" and "WallUpgradeVoucher" in m.voucher],
                         "武器未 L2 时不得备货墙升级券")
        self.assertTrue([m for m in missions if m.kind == "weapon"], "应优先武器 L2")


class TestFrontVoucherStock(unittest.TestCase):
    def test_d3_stocks_front_voucher_5(self):
        """D3+ 武器已 L2 → 正面墙券按需备货 ≥5。"""
        sim = make_sim(gold=400)
        sim.round_no = 261
        _add_weapons_l2(sim)
        cells, anchor = _front_cells(sim)
        for i, p in enumerate(cells[:6]):
            sim.roles.append(sim._role(74000 + i, p.x, p.y, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23), front="W")
        stock = [m for m in missions if m.kind == "stock" and m.voucher == "WallUpgradeVoucher1"]
        self.assertTrue(stock, "应备货正面墙 L1 升级券")
        self.assertGreaterEqual(stock[0].qty, 5)


class TestRepairerDayUpgrade(unittest.TestCase):
    def test_repairer_uses_voucher_in_day(self):
        """用户 2026-09-23：允许白天升级围墙（不闲置等晚上）。"""
        sim = make_sim(gold=100)
        sim.round_no = 271  # Day3 白天
        sim.roles.append(sim._role(75000, 12, 20, "wall", 1000, level=1))
        sim.role(W1)["backpack"] = ["WallUpgradeVoucher1"]
        sim.role(W1)["pos"] = {"x": 12, "y": 21}  # 墙旁
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        fsm.upgrade = (Pos(12, 20), "wall")
        ctx = type("C", (), {})()
        ctx.reserved = set()
        ctx.wall_registry = None
        ctx.home_anchor = Pos(9, 23)
        ctx.repair_anchor = Pos(12, 22)
        ctx.walls_left = 0
        ctx.robot_cells = ()
        ctx.night_now = False
        ctx.dusk_avoid = False
        ctx.mine_blacklist = {}
        ctx.price_boost_map = {}
        ctx.is_mine_blocked = lambda pos, rn: False
        ctx.note = lambda *a, **k: None
        cmd = fsm._upgrade_cmd(turn, unit, ctx, allow_use=True, allow_buy=True)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.get("action"), "use")
        self.assertEqual(cmd.get("name"), "WallUpgradeVoucher1")


if __name__ == "__main__":
    unittest.main()
