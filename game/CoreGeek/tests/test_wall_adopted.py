"""采纳的工人修复 A6 / A4 / A5 / A2（2026-09-24，用户批准）。

- A6：墙券按**自己持有**计数（别人的券不该阻止修理工购买）
- A4：备货任务买不起 → **清掉**（防卡死阻断墙升级）
- A5：金币不足 → **保留**墙升级任务（由采卖筹钱后再买）
- A2：黄昏站在出兵走廊/出生点 → 先走出来

（这些是独立小改，若影响通关可直接删除本文件 + 对应 4 处代码。）
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1, W2 = 10010, 10012


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class _Base(unittest.TestCase):
    def _setup(self, round_no=261, gold=300, wall_level=1):
        sim = make_sim(gold=gold)
        sim.round_no = round_no
        for y in (22, 23, 24, 25):    # 正面列（front='W' → dx=3）
            sim.roles.append(sim._role(90000 + y, 13, y, "wall", 1000, level=wall_level))
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
        return sim, turn, unit, fsm, ctx


class TestA6VoucherOwnCount(_Base):
    def test_other_units_voucher_does_not_block(self):
        sim, turn, unit, fsm, ctx = self._setup()
        sim.role(W2)["backpack"] = ["WallUpgradeVoucher1"] * 5   # 券在另一个工人身上
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        qty = fsm._voucher_qty(turn, unit, ctx, "WallUpgradeVoucher1", 20, "wall")
        self.assertGreater(qty, 0, "别人身上的墙券不应阻止修理工购买自己的")


class TestA4StockClears(_Base):
    def test_stock_clears_when_unaffordable(self):
        sim, turn, unit, fsm, ctx = self._setup(gold=0)
        fsm.upgrade = ("WallFixer", "stock", 15)
        cmd = fsm._upgrade_cmd(turn, unit, ctx, allow_use=True, allow_buy=True)
        self.assertIsNone(cmd)
        self.assertIsNone(fsm.upgrade, "买不起应清掉备货任务（否则卡死阻断墙升级）")


class TestA5KeepWallMission(_Base):
    def test_wall_mission_kept_when_broke(self):
        sim, turn, unit, fsm, ctx = self._setup(gold=0)
        fsm.upgrade = (Pos(13, 22), "wall")
        cmd = fsm._upgrade_cmd(turn, unit, ctx, allow_use=True, allow_buy=True)
        self.assertIsNone(cmd)
        self.assertIsNotNone(fsm.upgrade, "金币不足应保留墙升级任务（由采卖筹钱后再买）")


class TestA2DuskCorridorEscape(_Base):
    def test_worker_in_corridor_walks_out(self):
        sim, turn, unit, fsm, ctx = self._setup()
        ctx.dusk_avoid = True
        ctx.corridor_cells = (unit.pos,)
        ctx.spawn_cells = ()
        ctx.safe_anchor = Pos(10, 22)
        self.assertIsNotNone(fsm._dusk_corridor_escape(turn, unit, ctx),
                             "黄昏站在走廊里应先走出来")

    def test_no_escape_when_far(self):
        sim, turn, unit, fsm, ctx = self._setup()
        ctx.dusk_avoid = True
        ctx.corridor_cells = (Pos(35, 2),)
        ctx.spawn_cells = ()
        ctx.safe_anchor = Pos(10, 22)
        self.assertIsNone(fsm._dusk_corridor_escape(turn, unit, ctx))


if __name__ == "__main__":
    unittest.main()
