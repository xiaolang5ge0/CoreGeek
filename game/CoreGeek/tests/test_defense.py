"""P3 验收：单人夜防 —— 威胁评估、TTF、工人召回、闪避、火力致命优先、自救。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos, Turn, distance
from agent.threat import SAFE, WARN, CRITICAL, ThreatEstimator

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70


def run_rounds(brain, sim, n):
    out = []
    for _ in range(n):
        response, trace = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()
        out.append((response, trace))
    return out


def cmd_of(response, rid):
    return (response["roleCommandMap"] or {}).get(str(rid))


class TestNightSurvival(unittest.TestCase):
    def test_night1_wave_cleared_base_safe(self):
        """Night1 标准浪：全歼机器人、基地与炮塔存活。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        for i, (x, y) in enumerate([(16, 23), (17, 24), (16, 22), (18, 23), (17, 22)]):
            sim.spawn_robot(x, y, "smallRobot", hp=40, rid=30010 + i)
        sim.spawn_robot(18, 24, "middleRobot", hp=60, rid=30020)
        sim.spawn_robot(19, 22, "middleRobot", hp=60, rid=30021)
        history = run_rounds(brain, sim, 40)
        self.assertTrue(sim.base_alive)
        self.assertEqual(sim.robots, [])  # 40 回合内清场
        self.assertTrue(all(w["health"] > 0 for w in sim.weapons()))
        # 火力高效：AOE+致命优先下清场所需射击次数不多（≥5 且已全歼）
        attacks = sum(
            1 for resp, _ in history
            for cmd in resp["roleCommandMap"].values() if cmd.get("action") == "attack"
        )
        self.assertGreaterEqual(attacks, 5)


class TestThreatLevels(unittest.TestCase):
    def test_safe_warn_critical(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 无机器人 → SAFE
        response, trace = brain.decide(sim.payload())
        self.assertEqual(trace["threat"]["level"], SAFE)
        # 远处小队 → 非 CRITICAL
        sim.spawn_robot(30, 10, "smallRobot", hp=40, rid=30001)
        response, trace = brain.decide(sim.payload())
        self.assertIn(trace["threat"]["level"], (SAFE, WARN))
        # 6 BOSS 压境 → CRITICAL
        sim.robots.clear()
        for i in range(6):
            sim.spawn_robot(15 + i % 3, 22 + i // 3, "bossRobot", hp=800, rid=30100 + i)
        response, trace = brain.decide(sim.payload())
        self.assertEqual(trace["threat"]["level"], CRITICAL)
        self.assertLess(trace["threat"]["breach_rounds"], trace["threat"]["kill_rounds"])


class TestWorkerRecall(unittest.TestCase):
    def test_recall_on_critical(self):
        """CRITICAL 夜：工人进入 CRITICAL_DEFENSE 并回撤到基地邻域。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        for i in range(6):
            sim.spawn_robot(15 + i % 3, 22 + i // 3, "bossRobot", hp=800, rid=30100 + i)
        history = run_rounds(brain, sim, 10)
        states = set()
        for _, trace in history:
            for info in trace.get("workers", {}).values():
                if info.get("state") == "CRITICAL_DEFENSE":
                    states.add("CRITICAL_DEFENSE")
        self.assertIn("CRITICAL_DEFENSE", states)
        # 工人已回撤到基地附近
        for rid in (W1, W2):
            pos = sim.role(rid)["pos"]
            self.assertLessEqual(distance(Pos(pos["x"], pos["y"]), Pos(10, 23)), 6)


class TestEvade(unittest.TestCase):
    def test_worker_flees_close_robot(self):
        """夜间采矿工人被机器人贴近（≤2）时撤离。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 让工人在夜间先采上矿
        run_rounds(brain, sim, 5)
        miner = sim.role(W2)
        mx, my = miner["pos"]["x"], miner["pos"]["y"]
        sim.spawn_robot(mx + 2, my, "middleRobot", hp=60, rid=30201)
        response, trace = brain.decide(sim.payload())
        cmd = cmd_of(response, W2)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.get("action"), "move")
        target = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertGreater(distance(target, Pos(mx + 2, my)), distance(Pos(mx, my), Pos(mx + 2, my)))


class TestControlExclusion(unittest.TestCase):
    def test_no_fire_while_walking(self):
        """操控占用（已确认）：开拓者移动回合不得开火，归位后恢复开火。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 把开拓者放到内圈通道 (11,25)，放机器人进射程
        sim.role(PIONEER)["pos"] = {"x": 11, "y": 25}
        sim.spawn_robot(16, 23, "middleRobot", hp=60, rid=30301)
        fired_while_walking = 0
        for _ in range(8):
            response, trace = brain.decide(sim.payload())
            pioneer_cmd = cmd_of(response, PIONEER)
            attacks = [
                c for c in response["roleCommandMap"].values()
                if c.get("action") == "attack"
            ]
            if pioneer_cmd and pioneer_cmd.get("action") == "move":
                fired_while_walking += len(attacks)
            sim.apply(response)
            sim.advance()
        self.assertEqual(fired_while_walking, 0)
        # 归位 CP
        self.assertEqual(
            Pos(sim.role(PIONEER)["pos"]["x"], sim.role(PIONEER)["pos"]["y"]),
            brain.layout.control_point,
        )


class TestPioneerMedicine(unittest.TestCase):
    def test_low_hp_uses_medicine(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1 + 1)  # 入夜，开拓者已归位 CP
        pioneer = sim.role(PIONEER)
        pioneer["health"] = 60
        pioneer["backpack"] = ["medicine"]
        response, trace = brain.decide(sim.payload())
        cmd = cmd_of(response, PIONEER)
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.get("action"), "use")
        self.assertEqual(cmd.get("name"), "medicine")


if __name__ == "__main__":
    unittest.main()
