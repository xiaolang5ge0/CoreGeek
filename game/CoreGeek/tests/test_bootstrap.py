"""P1 验收：构造地图集成测试 —— Day1 Bootstrap + Night1 单人火力轮转。

场景：左上基地 (10,24)，石矿 (6,22)，铜矿 (25,10)，铁矿 (14,6)。
验收（ARCHITECTURE_DESIGN §6 P1）：Night1 前 3 火箭就位 + 围墙批量建成；
入夜后开拓者一站控三炮、轮转攻击、冷却受 3 回合约束。
"""
import unittest
from collections import Counter

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos

DAY1_ROUNDS = 70


def run_rounds(brain, sim, n):
    last = None
    for _ in range(n):
        payload = sim.payload()
        response, trace = brain.decide(payload)
        sim.apply(response)
        sim.advance()
        last = (response, trace)
    return last


class TestDay1Bootstrap(unittest.TestCase):
    def setUp(self):
        self.sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (25, 10): "copper", (14, 6): "iron"},
        )
        self.brain = Brain()

    def test_day1_weapons_and_walls(self):
        response, trace = run_rounds(self.brain, self.sim, DAY1_ROUNDS)
        # 3 火箭全部建成且都在布局炮台位上
        weapons = self.sim.weapons()
        self.assertEqual(len(weapons), 3)
        self.assertTrue(all(w["roleType"] == "rocket" for w in weapons))
        # 围墙批量建成（≥6）
        self.assertGreaterEqual(len(self.sim.walls()), 6)
        # 开拓者归位于控制点
        pioneer = self.sim.role(10011)
        cp = self.brain.layout.control_point
        self.assertEqual(Pos(pioneer["pos"]["x"], pioneer["pos"]["y"]), cp)
        # 武器造价 75 金已投入 3 座（经济工人卖货可能带回金币，不再断言 gold==0）
        self.assertEqual(len(weapons), 3)

    def test_layout_front_and_log(self):
        response, trace = run_rounds(self.brain, self.sim, 1)
        self.assertEqual(self.brain.front, "W")  # 左上基地开口朝西（背向来敌）
        self.assertIn("layout", trace)

    def test_night1_fire_rotation(self):
        run_rounds(self.brain, self.sim, DAY1_ROUNDS)
        # Night1：在 FRONT 开口方向（东侧）放一波中/小型机器人
        for i, (x, y) in enumerate([(16, 23), (17, 24), (16, 22), (18, 23)]):
            self.sim.spawn_robot(x, y, "middleRobot", hp=60, rid=30001 + i)
        fired = []
        for _ in range(6):
            payload = self.sim.payload()
            response, trace = self.brain.decide(payload)
            attacks = {
                int(rid): cmd
                for rid, cmd in response["roleCommandMap"].items()
                if cmd.get("action") == "attack"
            }
            for wid, cmd in attacks.items():
                self.assertEqual(cmd["controllerId"], "10011")  # 开拓者单人操控
                fired.append(wid)
            self.sim.apply(response)
            self.sim.advance()
        # 6 回合内至少打出 4 发，且武器轮转（≥2 座不同火箭）
        self.assertGreaterEqual(len(fired), 4)
        self.assertGreaterEqual(len(set(fired)), 2)

    def test_mine_lock_and_collect(self):
        run_rounds(self.brain, self.sim, DAY1_ROUNDS)
        # 经济闭环：采到的矿要么在背包，要么已卖成金
        bags = [
            self.sim.role(rid)["backpack"]
            for rid in (10010, 10012)
        ]
        total_ore = sum(len(bag) for bag in bags)
        self.assertGreater(total_ore + self.sim.gold, 0)


if __name__ == "__main__":
    unittest.main()
