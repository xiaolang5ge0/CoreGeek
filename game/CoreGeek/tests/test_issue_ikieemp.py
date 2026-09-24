"""issue IKIEMP 回归（2026-09-24，用户要求）。

日志（teamA 44）：挖矿工 D10 白天买了 Bomb/DizzyWeapon，但**整夜在外采矿从不使用**；
围墙修复包也只由维修工持有。修复三件事：

1. D10 挖矿工也自备 20 个 WallFixer（与维修工各 20，防卡位导致修墙不及时）；
2. D10 夜间持炸弹/眩晕 → **回防到锚点附近再使用**（优先大型机器人/BOSS）；
3. D10 夜间持 WallFixer → 就近抢修。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.brain import Brain, _Ctx
from agent.fsm_worker import (
    DAY10_FIXER_TARGET,
    ROLE_MINER,
    STATE_ITEM,
    STATE_RETURN,
    WorkerFSM,
)
from agent.planners.upgrade import FIXER_D10_PER_WORKER, UpgradePlanner
from agent.protocol import Pos, Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True

MINER = 10012
MAINTAINER = 10010
ANCHOR = Pos(10, 24)


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={}, shop=(25, 20))
    base.update(kw)
    return SimWorld(**base)


def day_round(day, k=30):
    return (day - 1) * 130 + k


def night_round(day, k=100):
    return (day - 1) * 130 + k


class TestDay10FixerStockFSM(unittest.TestCase):
    """D10 白天：挖矿工自备 20 个围墙修复包。"""

    def _setup(self, round_no, *, miner_packs=0):
        sim = make_sim(gold=500)
        sim.round_no = round_no
        sim.role(MAINTAINER)["backpack"] = ["WallFixer"] * 46
        sim.role(MINER)["backpack"] = ["WallFixer"] * miner_packs
        sim.role(MINER)["pos"] = {"x": 24, "y": 20}     # 站在商店旁
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == MINER)
        fsm = WorkerFSM(MINER)
        fsm.role = ROLE_MINER
        ctx = _Ctx({})
        ctx.reserved = set()
        return turn, unit, fsm, ctx

    def test_buys_20_on_day10(self):
        turn, unit, fsm, ctx = self._setup(day_round(10))
        cmd = fsm._day10_fixer_stock(turn, unit, ctx)
        self.assertIsNotNone(cmd, "D10 挖矿工应自备修复包")
        self.assertEqual(cmd["action"], "buy")
        self.assertEqual(cmd["name"], "WallFixer")
        self.assertEqual(cmd["num"], DAY10_FIXER_TARGET)

    def test_no_buy_before_day10(self):
        turn, unit, fsm, ctx = self._setup(day_round(9))
        self.assertIsNone(fsm._day10_fixer_stock(turn, unit, ctx))

    def test_no_buy_at_night(self):
        turn, unit, fsm, ctx = self._setup(night_round(10))
        self.assertIsNone(fsm._day10_fixer_stock(turn, unit, ctx))

    def test_no_buy_when_already_stocked(self):
        turn, unit, fsm, ctx = self._setup(day_round(10), miner_packs=DAY10_FIXER_TARGET)
        self.assertIsNone(fsm._day10_fixer_stock(turn, unit, ctx))


class TestDay10FixerStockPlan(unittest.TestCase):
    """D10 的目标改为**每工人各 20**（按最少持有者触发）。"""

    def _plan(self, day, miner_packs, maint_packs, gold=400):
        sim = make_sim(gold=gold)
        sim.round_no = day_round(day)
        sim.role(MINER)["backpack"] = ["WallFixer"] * miner_packs
        sim.role(MAINTAINER)["backpack"] = ["WallFixer"] * maint_packs
        turn = Turn.load(sim.payload())
        missions = UpgradePlanner().plan(turn, cp=Pos(9, 23))
        return [m for m in missions if m.kind == "stock" and m.voucher == "WallFixer"]

    def test_day10_triggers_when_a_worker_has_none(self):
        stock = self._plan(10, 0, 46)
        self.assertTrue(stock, "D10 有工人持 0 → 应触发修复包备货")
        self.assertEqual(stock[0].qty, FIXER_D10_PER_WORKER)

    def test_day10_no_trigger_when_both_have_20(self):
        self.assertFalse(self._plan(10, 20, 30), "两边都 ≥20 → 不再触发")

    def test_day9_keeps_global_sum(self):
        # D9 仍按全局求和：20+10=30 已达上限 → 不触发（保留旧行为）
        self.assertFalse(self._plan(9, 20, 10))


class TestDay10EmergencyItemsNight(unittest.TestCase):
    """D10 夜间：持炸弹/眩晕 → 回防到位后使用（优先大型/BOSS）。"""

    def _setup(self, round_no, miner_pos, *, big=True, item="Bomb"):
        sim = make_sim(gold=100)
        sim.round_no = round_no
        sim.role(MINER)["backpack"] = [item]
        sim.role(MINER)["pos"] = {"x": miner_pos[0], "y": miner_pos[1]}
        if big:
            sim.spawn_robot(12, 20, "largeRobot", hp=600, rid=30900)
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == MINER)
        fsm = WorkerFSM(MINER)
        fsm.role = ROLE_MINER
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.safe_anchor = ANCHOR
        return turn, unit, fsm, ctx

    def test_uses_item_when_home(self):
        turn, unit, fsm, ctx = self._setup(night_round(10), (11, 24))
        cmd = fsm._day10_use_item(turn, unit, ctx)
        self.assertIsNotNone(cmd, "回防到位应使用道具")
        self.assertEqual(cmd["action"], "use")
        self.assertEqual(cmd["name"], "Bomb")
        self.assertEqual(cmd["targetPos"][0], {"x": 12, "y": 20}, "优先砸大型机器人")
        self.assertEqual(fsm.state, STATE_ITEM)

    def test_returns_when_far(self):
        turn, unit, fsm, ctx = self._setup(night_round(10), (24, 20))
        self.assertIsNone(fsm._day10_use_item(turn, unit, ctx), "未到位不直接用")
        cmd = fsm._day10_return(turn, unit, ctx)
        self.assertIsNotNone(cmd, "持道具+有目标 → 回防")
        self.assertEqual(cmd["action"], "move")
        self.assertEqual(fsm.state, STATE_RETURN)

    def test_no_return_without_worthwhile_target(self):
        turn, unit, fsm, ctx = self._setup(night_round(10), (24, 20), big=False)
        self.assertIsNone(fsm._day10_return(turn, unit, ctx), "没有值得炸的目标就不回防")
        self.assertIsNone(fsm._day10_use_item(turn, unit, ctx))

    def test_no_item_before_day10(self):
        turn, unit, fsm, ctx = self._setup(night_round(9), (11, 24))
        self.assertIsNone(fsm._day10_use_item(turn, unit, ctx))
        self.assertIsNone(fsm._day10_return(turn, unit, ctx))

    def test_miner_dispatch_returns_command(self):
        turn, unit, fsm, ctx = self._setup(night_round(10), (24, 20))
        cmd = fsm._miner(turn, unit, ctx)
        self.assertIsNotNone(cmd, "D10 夜间应回防/用道具（不再整夜在外采矿）")


class TestDay10FlowE2E(unittest.TestCase):
    """端到端：D10 白天备包+买道具 → 夜间回防 → 用炸弹/眩晕。"""

    def test_miner_stocks_and_uses_items_on_day10(self):
        sim = make_sim(gold=800, mines={(6, 22): 'stone'})
        sim.add_mine((6, 22), 'stone', remaining=500)
        sim.round_no = day_round(10, 30)          # D10 白天
        sim.role(MAINTAINER)['backpack'] = ['WallFixer'] * 40
        sim.role(MINER)['backpack'] = []
        sim.role(MINER)['pos'] = {'x': 24, 'y': 20}   # 商店旁
        robots = ((30900, 12, 20, 'largeRobot', 900),
                  (30901, 12, 21, 'largeRobot', 900),
                  (30902, 11, 20, 'middleRobot', 300))
        brain = Brain()
        used: list[str] = []
        stocked = False
        for _ in range(140):
            in_day = ((sim.round_no - 1) % 130 + 1) <= 70
            if not in_day:                        # 夜袭持续，保持机器人存活
                have = {r['id'] for r in sim.robots}
                for rid, x, y, kind, hp in robots:
                    if rid not in have:
                        sim.spawn_robot(x, y, kind, hp=hp, rid=rid)
            response, _ = brain.decide(sim.payload())
            cmd = response['roleCommandMap'].get(str(MINER))
            if cmd and cmd.get('action') == 'use':
                used.append(cmd.get('name'))
            if sim.role(MINER)['backpack'].count('WallFixer') >= 20:
                stocked = True
            sim.apply(response)
            sim.advance()

        self.assertTrue(stocked, "D10 挖矿工应自备 20 个围墙修复包")
        self.assertIn('Bomb', used, "D10 夜间应使用范围炸弹打大型机器人/BOSS")
        self.assertIn('DizzyWeapon', used, "D10 夜间应使用眩晕法宝")


if __name__ == "__main__":
    unittest.main()