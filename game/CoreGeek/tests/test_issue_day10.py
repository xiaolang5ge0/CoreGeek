"""第 10 天紧急道具策略（用户 2026-09-24）。

**修复包已保障**（已买齐，或扣款后仍够买齐缺口）的前提下，挖矿工白天买
范围炸弹 / 眩晕法宝（打大型机器人/BOSS，提高击杀效率）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import _Ctx, BIG_ROBOTS
from agent.fsm_worker import WorkerFSM, ROLE_MINER
from agent.planners.upgrade import FIXER_STOCK_MAX
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

W1 = 10010
D10_DAY = 9 * 130 + 31   # 第 10 天白天（round_in_day=30）


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={}, shop=(25, 20))
    base.update(kw)
    return SimWorld(**base)


class TestDay10EmergencyItems(unittest.TestCase):
    def _setup(self, round_no, gold, packs):
        sim = make_sim(gold=gold)
        sim.round_no = round_no
        sim.role(W1)["backpack"] = ["WallFixer"] * packs
        sim.role(W1)["pos"] = {"x": 24, "y": 20}   # 站在商店旁
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_MINER
        ctx = _Ctx({})
        ctx.reserved = set()
        return turn, unit, fsm, ctx

    def test_buys_on_day10_when_packs_already_full(self):
        turn, unit, fsm, ctx = self._setup(D10_DAY, gold=200, packs=FIXER_STOCK_MAX)
        cmd = fsm._day10_bomb_buy(turn, unit, ctx)
        self.assertIsNotNone(cmd, "修复包已备齐 → 应买炸弹/眩晕")
        self.assertIn(cmd.get("name"), ("Bomb", "DizzyWeapon"))

    def test_buys_when_gold_covers_pack_reserve(self):
        # 修复包缺口 30 个 → 预留 300；金币 400，扣 100 后仍 >= 300
        turn, unit, fsm, ctx = self._setup(D10_DAY, gold=400, packs=0)
        self.assertEqual(fsm._wallfixer_reserve_gold(turn), FIXER_STOCK_MAX * 10)
        self.assertIsNotNone(fsm._day10_bomb_buy(turn, unit, ctx))

    def test_no_buy_when_would_break_pack_reserve(self):
        # 修复包缺口 30 → 预留 300；金币 350，扣 100 后只有 250 < 300
        turn, unit, fsm, ctx = self._setup(D10_DAY, gold=350, packs=0)
        self.assertIsNone(fsm._day10_bomb_buy(turn, unit, ctx),
                          "不得动用围墙修复包的钱")

    def test_no_buy_before_day10(self):
        turn, unit, fsm, ctx = self._setup(D10_DAY - 130, gold=999, packs=FIXER_STOCK_MAX)
        self.assertIsNone(fsm._day10_bomb_buy(turn, unit, ctx), "第 10 天前不买")

    def test_no_buy_at_night(self):
        turn, unit, fsm, ctx = self._setup(D10_DAY + 60, gold=999, packs=FIXER_STOCK_MAX)
        self.assertIsNone(fsm._day10_bomb_buy(turn, unit, ctx), "只在白天买")


class TestEmergencyItemTargetsBig(unittest.TestCase):
    def test_prefers_big_robot_cluster(self):
        """场上有大型/BOSS 时，应急道具优先砸它（Day10+ 不要求 3 只密度）。"""
        from agent.brain import Brain
        sim = make_sim(gold=500)
        sim.round_no = D10_DAY + 60   # 第 10 天夜
        sim.role(10011)["backpack"] = ["Bomb"]
        sim.spawn_robot(20, 20, "largeRobot", hp=500, rid=30900)
        sim.spawn_robot(30, 10, "smallRobot", hp=40, rid=30901)
        turn = Turn.load(sim.payload())
        pioneer = turn.pioneer()
        cmd = Brain()._emergency_item(turn, pioneer)
        self.assertIsNotNone(cmd, "Day10+ 有大型机器人应使用炸弹")
        self.assertEqual(cmd.get("action"), "use")
        self.assertEqual(cmd["targetPos"][0], {"x": 20, "y": 20}, "应砸大型机器人处")
        self.assertIn("largeRobot", BIG_ROBOTS)


if __name__ == "__main__":
    unittest.main()