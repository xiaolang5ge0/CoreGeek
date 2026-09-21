"""P4 验收：升级任务链（买券→用券）、优先级、受损墙升级回血、预算保留、矿黑名单。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
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
        run_rounds(brain, sim, 130 - DAY1 + 40)
        # 受损墙被升级（≥L2）且回满血；经济改善后可一路升到 L3
        self.assertGreaterEqual(wall["level"], 2)
        from agent.planners.upgrade import WALL_MAX_HP
        self.assertEqual(wall["health"], WALL_MAX_HP[wall["level"] - 1])


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
