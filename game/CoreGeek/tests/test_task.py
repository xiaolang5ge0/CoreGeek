"""P5 验收：任务流 —— Day1 接任务、LLM/沙盒异步、Multi Submit、黄昏返程。"""
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70

TASK = {
    "pos": (14, 14),
    "text": "任务：调用本地天气接口，查询北京今日天气",
    "scoreReward": 50,
    "goldReward": 30,
    "timeoutRounds": 60,
}


def plan_json(commands=None, answer=None, done=False):
    return json.dumps(
        {"analysis": "...", "commands": commands or [], "answer": answer, "done": done},
        ensure_ascii=False,
    )


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


class TestTaskAcceptAndComplete(unittest.TestCase):
    def test_day1_task_completed(self):
        """Day1 接任务 → LLM 一次出答案 → 提交成功拿奖励 → 黄昏前回 CP。"""
        llm = [plan_json(answer={"city": "北京", "weather": "晴"}, done=True)]
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            tasks=[TASK],
            llm_script=llm,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 任务完成：金币奖励到账、任务点进入冷却
        self.assertGreaterEqual(sim.gold, 30)
        self.assertGreaterEqual(sim.score, 50)
        self.assertTrue(sim.submissions)  # 有过提交
        self.assertTrue(sim.prompts_seen)  # 任务期间用了 LLM（免费额度）
        # 开拓者已归位 CP 守夜
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)


class TestSandboxFlow(unittest.TestCase):
    def test_explore_then_answer(self):
        """LLM 计划带沙盒命令：executeCmd 逐条下发 → 汇编答案 → 提交。"""
        llm = [
            plan_json(commands=["ls /data", "cat /data/api.txt"], answer=None),
            plan_json(answer={"city": "北京"}, done=True),
        ]
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            tasks=[TASK],
            llm_script=llm,
            cmd_handler=lambda cmd: "[exitCode:0]\n北京 晴 25C",
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertEqual(len(sim.cmds_seen), 2)  # 两条探索命令都执行了
        self.assertTrue(sim.submissions)
        self.assertGreaterEqual(sim.score, 50)


class TestDuskReturn(unittest.TestCase):
    def test_abort_task_before_night(self):
        """任务做不完也必须入夜前回家（离开任务点=任务结束）。"""
        never_done = [plan_json(commands=[], answer=None, done=False)] * 100
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (8, 20): "copper"},
            tasks=[TASK],
            llm_script=never_done,
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)  # 入夜前已归位
        self.assertEqual(sim.phase_task, "")  # 任务因离开而结束


class TestNoTaskFallback(unittest.TestCase):
    def test_no_tasks_keeps_guard(self):
        """无任务点：开拓者白天守家（P1 行为不回退）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"})
        brain = Brain()
        run_rounds(brain, sim, 20)
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)


if __name__ == "__main__":
    unittest.main()
