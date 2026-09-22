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


class TestBigWaveNight1(unittest.TestCase):
    def test_survive_70_robot_wave(self):
        """复刻 TeamB 实战：Day1 夜潮 60小+10中，正面墙优先+贴脸开火 → 基地零伤。"""
        import random

        sim = SimWorld(station_pos=(30, 10), mines={(35, 13): "stone", (35, 15): "copper", (18, 3): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 正面（西/迎敌侧）墙优先
        west = [w for w in sim.walls() if w["pos"]["x"] <= 28]
        self.assertGreaterEqual(len(west), 4)
        random.seed(7)
        rid = 30000
        for _ in range(60):
            sim.spawn_robot(random.randint(18, 22), random.randint(6, 14), "smallRobot", hp=40, rid=rid)
            rid += 1
        for _ in range(10):
            sim.spawn_robot(random.randint(19, 23), random.randint(7, 13), "middleRobot", hp=60, rid=rid)
            rid += 1
        attacks = 0
        for _ in range(60):
            response, trace = brain.decide(sim.payload())
            attacks += sum(1 for c in response["roleCommandMap"].values() if c.get("action") == "attack")
            sim.apply(response)
            sim.advance()
        self.assertTrue(sim.base_alive)
        self.assertGreaterEqual(sim.role(10013)["health"], 1400)  # 几乎零伤
        self.assertGreaterEqual(attacks, 40)  # 火力全开（实战仅 17 次的反面）


class TestWorkerRecall(unittest.TestCase):
    def test_recall_only_when_robot_close(self):
        """新语义（事实：机器人主攻基地、顺路才杀工人）：仅机器人贴近工人(≤3)才召回，
        远处打基地的兵潮不召回工人（避免过度召回震荡）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 6 BOSS 在远处（距工人 >3）打基地 → 工人不应被召回
        for i in range(6):
            sim.spawn_robot(16 + i % 3, 22 + i // 3, "bossRobot", hp=800, rid=30100 + i)
        response, trace = brain.decide(sim.payload())
        states = {info.get("state") for info in (trace.get("workers") or {}).values()}
        self.assertNotIn("CRITICAL_DEFENSE", states, "远处兵潮不应召回工人")

    def test_recall_when_robot_adjacent(self):
        """机器人贴到工人 ≤3 格 → 该工人进入 CRITICAL_DEFENSE 召回。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        sim.add_mine((8, 20), "copper", remaining=30)
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        run_rounds(brain, sim, 5)  # 进入夜间，工人在外采矿
        miner = sim.role(W2)
        mx, my = miner["pos"]["x"], miner["pos"]["y"]
        sim.spawn_robot(mx + 1, my, "middleRobot", hp=60, rid=30201)  # 贴脸
        recalled = False
        for _ in range(4):
            response, trace = brain.decide(sim.payload())
            info = (trace.get("workers") or {}).get(str(W2)) or {}
            if info.get("state") == "CRITICAL_DEFENSE":
                recalled = True
            sim.apply(response)
            sim.advance()
        self.assertTrue(recalled, "机器人贴脸时工人应被召回")


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
        # 核心行为：机器人逼近期间工人停止采集（不再送死），进入防御态
        collects = 0
        states = set()
        for _ in range(8):
            response, trace = brain.decide(sim.payload())
            cmd = cmd_of(response, W2) or {}
            if cmd.get("action") == "collect":
                collects += 1
            info = (trace.get("workers") or {}).get(str(W2)) or {}
            if info.get("state"):
                states.add(info["state"])
            sim.apply(response)
            sim.advance()
        self.assertEqual(collects, 0, "危险圈内不得继续采集")
        self.assertTrue(
            states & {"CRITICAL_DEFENSE", "EVADE", "NIGHT_REPAIR"},
            f"工人应进入防御态，实际 {states}",
        )


class TestFireAdjacentRobot(unittest.TestCase):
    def test_fire_at_robot_next_to_building(self):
        """机器人贴脸己方建筑时也必须开火（实战复盘：原友伤惩罚导致全面哑火）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 机器人直接贴到基地旁（模拟破墙后贴脸）
        sim.spawn_robot(11, 22, "smallRobot", hp=40, rid=30901)
        response, trace = brain.decide(sim.payload())
        attacks = [
            c for c in response["roleCommandMap"].values() if c.get("action") == "attack"
        ]
        self.assertTrue(attacks, "贴脸机器人必须开火")
        self.assertNotEqual(trace.get("fire", {}).get("reason"), "no_valuable_target")


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
        # 归位 CP 或（机器人逼近时）撤往内圈——两者都合法，关键是不在移动回合开火
        pos = Pos(sim.role(PIONEER)["pos"]["x"], sim.role(PIONEER)["pos"]["y"])
        robot = Pos(16, 23)
        from agent.protocol import distance as _d
        self.assertTrue(
            pos == brain.layout.control_point or _d(pos, robot) > 3,
            f"pioneer {pos} 应归位 CP 或远离机器人",
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
