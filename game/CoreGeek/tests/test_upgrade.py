"""P4 验收：升级任务链（买券→用券）、优先级、受损墙升级回血、预算保留、矿黑名单。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain, _Ctx
from agent.fsm_pioneer import PioneerFSM, STATE_WEAPON_BUY, STATE_WEAPON_UPGRADE
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Pos, Turn

W1, W2 = 10010, 10012
DAY1 = 70


def run_rounds(brain, sim, n):
    out = []
    for _ in range(n):
        response, trace = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()
        out.append((response, trace))
    return out


def build_day1(brain, sim):
    run_rounds(brain, sim, DAY1)


class TestWeaponUpgrade(unittest.TestCase):
    def test_rocket_upgraded_to_level2(self):
        """金币到位后，工人完成买券+用券，火箭 L1→L2 且回满血。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        build_day1(brain, sim)
        rocket = sim.weapons()[0]
        rocket["health"] = 500  # 模拟受损
        sim.gold = 200
        # 跑过 Night1 进入 Day2，升级任务应在白天完成（买券+用券行程较长）
        run_rounds(brain, sim, 130 - DAY1 + 40)
        levels = sorted(w["level"] for w in sim.weapons())
        self.assertIn(2, levels)
        upgraded = [w for w in sim.weapons() if w["level"] == 2][0]
        self.assertEqual(upgraded["health"], 1500)  # 升级回满血


class TestUpgradePriority(unittest.TestCase):
    def test_weapon_before_wall(self):
        """武器优先于受损墙（预算仅够其一）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        build_day1(brain, sim)
        wall = sim.walls()[0]
        wall["health"] = 300  # 受损墙
        sim.gold = 130  # 武器券100+保留30，无余给墙
        planner = UpgradePlanner()
        missions = planner.plan(Turn.load(sim.payload()))
        self.assertEqual(len(missions), 1)
        self.assertEqual(missions[0].kind, "weapon")

    def test_reserve_kept(self):
        """预算保留：金 100（保留 30）买不起武器券 100 → 无武器任务（廉价墙任务允许）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        brain = Brain()
        build_day1(brain, sim)
        sim.gold = 100
        planner = UpgradePlanner()
        missions = planner.plan(Turn.load(sim.payload()))
        self.assertFalse(any(m.kind == "weapon" for m in missions))
        self.assertTrue(all(m.cost <= 100 - 30 for m in missions))


class TestWallRepairViaUpgrade(unittest.TestCase):
    def test_damaged_wall_upgraded(self):
        """受损墙（血量比例<0.6）获得升级任务并回血。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        build_day1(brain, sim)
        # 打残所有火箭让武器任务不出现，只留墙
        for weapon in sim.weapons():
            weapon["level"] = 3
        wall = sim.walls()[0]
        wall["health"] = 400  # L1 比例 0.4
        sim.gold = 60
        run_rounds(brain, sim, 300)  # 跑到 Day3+（修理工 D3+ 才做墙升级）
        # 受损墙被升级（≥L2）且回满血
        self.assertGreaterEqual(wall["level"], 2)
        from agent.planners.upgrade import WALL_MAX_HP
        self.assertEqual(wall["health"], WALL_MAX_HP[wall["level"] - 1])


class TestNightWallUpgrade(unittest.TestCase):
    def test_wall_upgrade_happens_at_night_not_day(self):
        """用户策略：白天只采购/采集，围墙升级（=回血）留到夜间执行。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        build_day1(brain, sim)
        for weapon in sim.weapons():          # 武器先满级，解除"武器优先"对墙升级的门控
            weapon["level"] = 3
            weapon["health"] = 2000
        wall = sim.walls()[0]
        wall["health"] = 500                  # <60% 受损
        sim.gold = 300
        day_uses = night_uses = 0
        for _ in range(320):
            is_day = sim._is_day()
            response, _ = brain.decide(sim.payload())
            for cmd in response["roleCommandMap"].values():
                if cmd.get("action") == "use" and "WallUpgrade" in str(cmd.get("name")):
                    if is_day:
                        day_uses += 1
                    else:
                        night_uses += 1
            sim.apply(response)
            sim.advance()
        self.assertEqual(day_uses, 0, "白天不应用券升级墙（应留给夜间）")
        self.assertGreaterEqual(night_uses, 1, "夜间应执行围墙升级（升级=回血）")


class TestPioneerBatchPurchase(unittest.TestCase):
    def test_buys_current_level_vouchers_in_one_trip(self):
        """issue#25：金币够时应一次买齐当前所需券（V1+V2），而不是买一张就回去升级再出来。"""
        sim = SimWorld(station_pos=(10, 24), mines={}, gold=272, shop=(25, 20))
        sim.roles = [r for r in sim.roles if r["roleType"] != "station"]
        sim.roles.append(sim._role(50013, 10, 24, "station", 1500, level=1))
        sim.roles.append(sim._role(50040, 8, 20, "rocket", 1000, level=1))
        sim.roles.append(sim._role(50041, 8, 22, "rocket", 1500, level=2))
        sim.roles.append(sim._role(50042, 9, 20, "rocket", 1500, level=2))
        sim.role(10011)["pos"] = {"x": 24, "y": 19}  # 商店旁
        fsm = PioneerFSM()
        ctx = _Ctx({})
        ctx.reserved = set()
        ctx.gunner_upgrade = (Pos(8, 20), "weapon")   # 目标=最低级武器 L1
        buys = []
        for _ in range(4):
            turn = Turn.load(sim.payload())
            pioneer = next(u for u in turn.ours if u.kind == "pioneer")
            cmd = fsm._weapon_upgrade_cmd(turn, pioneer, ctx)
            if cmd and cmd.get("action") == "buy":
                buys.append((cmd["name"], cmd.get("num")))
                sim.role(10011)["backpack"].append(cmd["name"])
                sim.gold -= {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150}[cmd["name"]]
            else:
                break
        self.assertEqual([b[0] for b in buys],
                         ["WeaponUpgradeVoucher1", "WeaponUpgradeVoucher2"],
                         "应一次买齐 V1 + V2")
        self.assertEqual(sim.gold, 22)

    def test_buy_qty_zero_when_unaffordable(self):
        """买不起时数量为 0（不得发出非法 buy）。"""
        sim = SimWorld(station_pos=(10, 24), mines={}, gold=10, shop=(25, 20))
        sim.roles.append(sim._role(50040, 8, 20, "rocket", 1000, level=1))
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        fsm = PioneerFSM()
        self.assertEqual(fsm._buy_qty(turn, pioneer, "WeaponUpgradeVoucher1", 100), 0)


class TestRepairerReturnHome(unittest.TestCase):
    def test_repairer_returns_home_before_dusk(self):
        """issue#25：修理工黄昏必须归位（此前 ctx.home_anchor 在工人阶段为 None → 卡墙外）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        sim.add_mine((6, 22), "stone", remaining=120)
        sim.add_mine((7, 26), "stone", remaining=120)
        brain = Brain()
        build_day1(brain, sim)
        sim.role(W1)["pos"] = {"x": 22, "y": 30}  # 把修理工丢到远处墙外
        sim.gold = 0                               # 排除采购干扰
        sim.round_no = 321                         # Day3 白天后段（距天黑 10 回合）
        cp = brain.layout.control_point
        states = set()
        for _ in range(8):
            response, trace = brain.decide(sim.payload())
            info = (trace.get("workers") or {}).get(str(W1)) or {}
            states.add(info.get("state"))
            sim.apply(response)
            sim.advance()
        self.assertIn("RETURN_HOME", states, "修理工应在黄昏前进入归位状态")
        pos = sim.role(W1)["pos"]
        self.assertLess(max(abs(pos["x"] - cp.x), abs(pos["y"] - cp.y)), 20, "应朝基地移动")


class TestMineBlacklist(unittest.TestCase):
    def test_collect_failure_blacklists_mine(self):
        """新闻封矿：采集失败后该矿被拉黑，工人改选/闲置而不再重复采。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        sim.add_mine((8, 20), "copper", remaining=30)  # 矿量充足，聚焦封矿逻辑
        brain = Brain()
        # 先让工人锁定铜矿
        run_rounds(brain, sim, 15)
        self.assertIn((8, 20), sim.mines)
        sim.closed_mines.add((8, 20))
        collects_after = 0
        for _ in range(6):
            response, trace = brain.decide(sim.payload())
            for cmd in response["roleCommandMap"].values():
                if cmd.get("action") == "collect":
                    collects_after += 1
            sim.apply(response)
            sim.advance()
        self.assertTrue(brain.mine_blacklist.get(Pos(8, 20), 0) > 0)
        # 拉黑后不再对该矿发 collect（最多只有反馈前的 1 次）
        self.assertLessEqual(collects_after, 2)


if __name__ == "__main__":
    unittest.main()
