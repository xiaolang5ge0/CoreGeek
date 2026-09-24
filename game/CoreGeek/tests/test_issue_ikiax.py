"""issue IKIAXN 回归（2026-09-24）：白天后勤模式（卖货 + 一次买齐）。

用户要求：
1. 维修工白天第 45 回合起就开始卖资源/买道具，预留好回合数。
2. 不要只买一样；墙券数量按"当前墙等级 vs 目标等级"的差距定（不被总数上限卡死），
   并同时买 WallFixer，只要不超过背包限额。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1 = 10010


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={}, shop=(25, 20))
    base.update(kw)
    return SimWorld(**base)


class TestDayLogistics(unittest.TestCase):
    def _setup(self, round_no, gold=300, wall_level=1):
        sim = make_sim(gold=gold)
        sim.round_no = round_no
        for y in (22, 23, 24, 25):    # 正面列（front='W' → dx=3）
            sim.roles.append(sim._role(90000 + y, 13, y, "wall", 1000, level=wall_level))
        sim.role(W1)["pos"] = {"x": 24, "y": 20}   # 站在商店旁
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.layout_anchor = (10, 23)
        ctx.layout_front = "W"
        return turn, unit, fsm, ctx

    def test_shopping_list_covers_vouchers_and_packs(self):
        turn, unit, fsm, ctx = self._setup(261)   # D3，正面 4 面 L1 墙
        items = dict(fsm._shopping_list(turn, unit, ctx))
        self.assertIn("WallUpgradeVoucher1", items, "应按 L1 墙数量备 V1")
        self.assertGreaterEqual(items["WallUpgradeVoucher1"], 4, "不被固定 5 张上限卡死")
        self.assertIn("WallFixer", items, "应同时买修复包（不买一类就走）")
        self.assertLessEqual(items["WallFixer"], 8, "D1–D4 修复包 ≤8")

    def test_shopping_list_v2_for_l2_target3(self):
        turn, unit, fsm, ctx = self._setup(521, wall_level=2)   # D5，正面 L2 → 目标 L3
        items = dict(fsm._shopping_list(turn, unit, ctx))
        self.assertIn("WallUpgradeVoucher2", items)

    def test_logistics_active_from_round_45(self):
        turn, unit, fsm, ctx = self._setup(261 + 45)   # D3 第 45 回合
        cmd = fsm._logistics(turn, unit, ctx)
        self.assertIsNotNone(cmd, "第 45 回合起应进入后勤模式")
        self.assertIn(cmd["action"], ("buy", "move", "sell"))

    def test_logistics_inactive_before_45(self):
        turn, unit, fsm, ctx = self._setup(261 + 30)   # D3 第 30 回合
        self.assertIsNone(fsm._logistics(turn, unit, ctx))

    def test_logistics_inactive_at_night(self):
        turn, unit, fsm, ctx = self._setup(261 + 75)   # D3 夜
        self.assertIsNone(fsm._logistics(turn, unit, ctx))


if __name__ == "__main__":
    unittest.main()
