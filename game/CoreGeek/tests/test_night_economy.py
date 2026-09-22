"""第六轮实战修复回归：夜间采矿(不蹲防)/脱离后恢复/不选敌方侧矿/LLM多行解析。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.planners.task import TaskPlanner, TaskSession
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


def cmd_of(response, rid):
    return (response["roleCommandMap"] or {}).get(str(rid))


class TestNightMining(unittest.TestCase):
    def test_both_workers_mine_night1(self):
        """Night1 相对轻松：两工人都应采矿，不被修墙岗/召回锁住（问题1/2）。"""
        sim = SimWorld(station_pos=(10, 24),
                       mines={(6, 22): "stone", (8, 20): "copper", (4, 30): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        sim.add_mine((6, 22), "stone", remaining=30)
        sim.add_mine((8, 20), "copper", remaining=30)
        # 机器人在远处（不贴脸）
        sim.spawn_robot(30, 20, "smallRobot", hp=40, rid=30801)
        mine_rounds = 0
        repair_rounds = 0
        for _ in range(12):
            response, trace = brain.decide(sim.payload())
            states = (trace.get("workers") or {})
            if any(c.get("action") == "collect" for c in response["roleCommandMap"].values()):
                mine_rounds += 1
            if any((v or {}).get("state") == "NIGHT_REPAIR" for v in states.values()):
                repair_rounds += 1
            sim.apply(response)
            sim.advance()
        self.assertGreater(mine_rounds, 5, "Night1 工人应大量采矿")
        self.assertEqual(repair_rounds, 0, "Night1 不应启用修墙岗")

    def test_resume_mining_after_robots_gone(self):
        """机器人清空后工人应恢复采矿，不再蹲防（问题2）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 机器人贴脸 → 触发规避
        miner = sim.role(W2)
        mx, my = miner["pos"]["x"], miner["pos"]["y"]
        sim.spawn_robot(mx + 1, my, "middleRobot", hp=60, rid=30802)
        run_rounds(brain, sim, 3)
        # 机器人消失 + 补矿
        sim.robots.clear()
        sim.add_mine((6, 22), "stone", remaining=30)
        sim.add_mine((8, 20), "copper", remaining=30)
        mined = False
        for _ in range(8):
            response, trace = brain.decide(sim.payload())
            if any(c.get("action") == "collect" for c in response["roleCommandMap"].values()):
                mined = True
                break
            sim.apply(response)
            sim.advance()
        self.assertTrue(mined, "机器人清空后应恢复采矿")


class TestMineSidePreference(unittest.TestCase):
    def test_never_pick_enemy_side_mine(self):
        """我方侧有矿时，绝不选敌方侧矿（问题4：避免被兵潮顺路打死）。"""
        sim = SimWorld(station_pos=(10, 24),
                       mines={(6, 22): "stone", (8, 20): "copper", (35, 28): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        sim.add_mine((6, 22), "stone", remaining=50)
        sim.add_mine((8, 20), "copper", remaining=50)
        for _ in range(20):
            response, trace = brain.decide(sim.payload())
            for info in (trace.get("workers") or {}).values():
                m = info.get("mine")
                if m:
                    self.assertLessEqual(m["x"], 25, f"不应锁定敌方侧矿 {m}")
            sim.apply(response)
            sim.advance()

    def test_far_mine_deprioritized(self):
        """只有敌方侧矿时仍不选（MAX_MINE_DIST 约束）：工人宁可空闲也不深入。"""
        sim = SimWorld(station_pos=(10, 24), mines={(35, 28): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        sim.add_mine((35, 28), "stone", remaining=50)
        locked_far = False
        for _ in range(10):
            response, trace = brain.decide(sim.payload())
            for info in (trace.get("workers") or {}).values():
                m = info.get("mine")
                if m and m["x"] > 25:
                    locked_far = True
            sim.apply(response)
            sim.advance()
        self.assertFalse(locked_far, "超出 MAX_MINE_DIST 的敌方侧矿不应被选")


class TestLLMParsing(unittest.TestCase):
    def test_answer_after_explanation(self):
        """LLM 输出带前缀说明 + 多行时，仍能提取 ANSWER（问题3：LLM 兜底健壮性）。"""
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        planner._on_llm_result(s, '根据证据分析如下：\nANSWER: {"city": "北京", "total_count": 12}\n完成。')
        self.assertEqual(s.best_answer, {"city": "北京", "total_count": 12})

    def test_cmd_after_explanation(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        planner._on_llm_result(s, '执行：\nCMD: ls /data && cat /data/api.txt')
        self.assertEqual(s.pending_llm_cmd, "ls /data && cat /data/api.txt")

    def test_bare_json_as_answer(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.llm_pending = True
        planner._on_llm_result(s, '{"total_count": 5}')
        self.assertEqual(s.best_answer, {"total_count": 5})


class TestWallRebuild(unittest.TestCase):
    def test_destroyed_wall_rebuilt_next_day(self):
        """墙被拆后，次日白天应重建（墙环不能长期缺口）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertEqual(len(sim.walls()), 14)
        # 模拟夜战拆掉 3 面墙（直接移除角色）
        destroyed = sim.walls()[:3]
        sim.roles = [r for r in sim.roles if r not in destroyed]
        self.assertEqual(len(sim.walls()), 11)
        # 次日白天补矿重建（跑满夜+一整天 = 130 回合，确保有足够白天回合）
        sim.add_mine((6, 22), "stone", remaining=40)
        sim.add_mine((7, 26), "stone", remaining=40)
        run_rounds(brain, sim, 130)
        self.assertGreaterEqual(len(sim.walls()), 13, "被拆的墙应重建")


if __name__ == "__main__":
    unittest.main()
