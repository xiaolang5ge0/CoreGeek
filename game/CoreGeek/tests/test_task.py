"""P5 任务流测试（严格按 issue#21 策略：7 阶段 FSM + LLM-JSON 协议 + SOP）。"""
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
    "text": "请阅读task_1_beijing.md，获取任务信息",
    "scoreReward": 80,
    "goldReward": 80,
    "timeoutRounds": 15,
}

FIND_RESULT = "[exitCode:0]\n/tmp/selfEvolutionTask/1-unknown-api/task_1_beijing.md\n"
READ_RESULT = "[exitCode:0]\n# 任务：查询北京文化遗产\nAPI 文档：http://localhost:8899/api/heritage\n"
CURL_RESULT = '[exitCode:0]\n{"records":[{"name":"故宫","era":"明","type":"古建筑"}]}\n'
ANSWER = '{"city": "北京", "total_count": 1}'


def llm_cmd(cmd):
    return json.dumps({"cmd": cmd, "answer": "", "isFinished": False}, ensure_ascii=False)


def llm_answer(ans):
    return json.dumps({"cmd": "", "answer": ans, "isFinished": True}, ensure_ascii=False)


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
    base.update(kw)
    return SimWorld(**base)


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


class TestTaskFlow(unittest.TestCase):
    def test_find_read_llm_submit(self):
        """完整链路：find→cat→LLM(cmd)→curl→LLM(answer)→submit。"""
        def handler(cmd):
            if cmd.startswith("find /"):
                return FIND_RESULT
            if cmd.startswith("cat "):
                return READ_RESULT
            if "curl" in cmd:
                return CURL_RESULT
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[TASK],
            llm_script=[llm_cmd("curl http://localhost:8899/api/heritage?location=北京"),
                        llm_answer(ANSWER)],
            cmd_handler=handler,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 80)
        self.assertTrue(sim.submissions)
        self.assertIn("北京", sim.submissions[0])

    def test_non_json_three_times_force_end(self):
        """连续 3 次非 JSON → 强制结束（不提交）。"""
        sim = make_sim(
            tasks=[TASK],
            llm_script=["这不是JSON", "还是不是", "仍然不是", "第四次也不该被用到"],
            cmd_handler=lambda cmd: FIND_RESULT if cmd.startswith("find") else READ_RESULT,
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertEqual(sim.submissions, [])  # 未提交
        self.assertEqual(brain.task_planner.completed, 0)

    def test_json_tolerant_parse(self):
        """LLM 回复带前后缀说明时仍能提取 JSON（容忍解析）。"""
        wrapped = "好的，以下是结果：\n" + llm_answer(ANSWER) + "\n请查收"
        sim = make_sim(
            tasks=[TASK],
            llm_script=[wrapped],
            cmd_handler=lambda cmd: FIND_RESULT if cmd.startswith("find") else READ_RESULT,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.submissions)


class TestSopEvolution(unittest.TestCase):
    def test_sop_extracted_after_completion(self):
        """完成任务后提取 SOP（供第二天复用）。"""
        sim = make_sim(
            tasks=[TASK],
            llm_script=[llm_answer(ANSWER)],
            cmd_handler=lambda cmd: FIND_RESULT if cmd.startswith("find") else READ_RESULT,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(brain.task_planner.sop, "应提取 SOP")


class TestAcceptFailNoRetry(unittest.TestCase):
    def test_accept_fail_never_retry(self):
        """acceptTask FAIL（errorCode 4 红线）→ 立即放弃，绝不再试。"""
        sim = make_sim(tasks=[TASK], accept_fails=True)
        brain = Brain()
        run_rounds(brain, sim, 30)
        self.assertEqual(sim.accept_count, 1)


class TestNoTaskFallback(unittest.TestCase):
    def test_no_tasks_keeps_guard(self):
        sim = make_sim()
        brain = Brain()
        run_rounds(brain, sim, 20)
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)


if __name__ == "__main__":
    unittest.main()
