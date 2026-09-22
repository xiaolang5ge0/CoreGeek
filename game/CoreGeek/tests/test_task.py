"""P5 任务流测试（健壮探索 + 任务期 LLM 不限次 + 命令加固）。

覆盖：
- 单条命令健壮探索 → LLM(cmd) → LLM(answer) → submit（API 类）。
- 任务期 LLM 不限次（额度耗尽仍发 prompt，issue#23/#24 根因回归）。
- 工程类：确定性探测（去 CRLF+./check）→ TOKEN 自动提交（零 LLM）。
- CRLF/漏 cd 失败 → 确定性重试。
- 连续非 JSON → 强制结束；JSON 容忍解析；SOP 提取；acceptTask 不重试。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70

API_TASK = {
    "pos": (14, 14),
    "text": "请阅读task_1_beijing.md，获取任务信息",
    "scoreReward": 80,
    "goldReward": 80,
    "timeoutRounds": 15,
}

ENG_TASK = {
    "pos": (14, 14),
    "text": "任务：修复 ws_3 工程，通过 ./check",
    "scoreReward": 50,
    "goldReward": 30,
    "timeoutRounds": 20,
}

API_EXPLORE = (
    "[exitCode:0]\n"
    "__FILE:/tmp/selfEvolutionTask/1-unknown-api/task_1_beijing.md\n"
    "=== TASK ===\n# 任务：查询北京文化遗产\nAPI 文档：http://localhost:8899/api/heritage\n"
    "=== FILE:/tmp/selfEvolutionTask/1-unknown-api/API_DOCS.md ===\n"
    "base http://localhost:8899\nX-API-Key: he123\n"
    "=== LIST ===\n/tmp/selfEvolutionTask/1-unknown-api/API_DOCS.md 644\n"
    "__DIR:/tmp/selfEvolutionTask/1-unknown-api\n"
)

ENG_EXPLORE = (
    "[exitCode:0]\n"
    "__FILE:/tmp/selfEvolutionTask/ws_3/task_ws3.md\n"
    "=== TASK ===\n任务：修复 ws_3 工程，通过 ./check\n"
    "=== FILE:/tmp/selfEvolutionTask/ws_3/spec.md ===\n目录 data 权限 755\n"
    "=== LIST ===\n/tmp/selfEvolutionTask/ws_3/check 755\n"
    "__DIR:/tmp/selfEvolutionTask/ws_3\n"
)

ENG_PROBE_FAIL = (
    "[exitCode:0]\n__WS:/tmp/selfEvolutionTask/ws_3\n"
    "[FAIL] DIR data — 期望 exists,755\n"
)

ENG_PROBE_OK = "[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: abc123token\n"
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


class TestTaskFlow(unittest.TestCase):
    def test_explore_llm_cmd_answer_submit(self):
        """API 类：健壮探索 → LLM(curl) → LLM(answer) → submit。"""
        def handler(cmd):
            if "find /tmp/selfEvolutionTask" in cmd:
                return API_EXPLORE
            if "curl" in cmd:
                return CURL_RESULT
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[API_TASK],
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

    def test_explore_command_is_robust(self):
        """探索命令须一次覆盖：定位 + 读文档 + 列权限（用户模板）。"""
        captured = []

        def handler(cmd):
            captured.append(cmd)
            return API_EXPLORE if "find /tmp/selfEvolutionTask" in cmd else "[exitCode:0]\n"

        sim = make_sim(tasks=[API_TASK], llm_script=[llm_answer(ANSWER)],
                       cmd_handler=handler, expected_answer="北京")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        explore = next(c for c in captured if "find /tmp/selfEvolutionTask" in c)
        for token in ("-iname", "API_DOCS.md", "ws_*/spec.md", "__DIR:", "=== LIST ==="):
            self.assertIn(token, explore)

    def test_non_json_three_times_force_end(self):
        """连续 3 次非 JSON → 强制结束（不提交）。"""
        sim = make_sim(
            tasks=[API_TASK],
            llm_script=["这不是JSON", "还是不是", "仍然不是", "第四次也不该被用到"],
            cmd_handler=lambda cmd: API_EXPLORE if "find /tmp" in cmd else "[exitCode:0]\n",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertEqual(sim.submissions, [])
        self.assertEqual(brain.task_planner.completed, 0)

    def test_json_tolerant_parse(self):
        """LLM 回复带前后缀说明时仍能提取 JSON（容忍解析）。"""
        wrapped = "好的，以下是结果：\n" + llm_answer(ANSWER) + "\n请查收"
        sim = make_sim(
            tasks=[API_TASK],
            llm_script=[wrapped],
            cmd_handler=lambda cmd: API_EXPLORE if "find /tmp" in cmd else "[exitCode:0]\n",
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.submissions)


class TestTaskLlmUnlimited(unittest.TestCase):
    def test_prompt_sent_even_when_daily_budget_exhausted(self):
        """issue#23/#24 根因回归：任务期 LLM 不限次（额度耗尽仍发 prompt，且不计数）。"""
        sim = make_sim(
            tasks=[API_TASK],
            llm_script=[llm_cmd("curl http://localhost:8899/x"),
                        llm_answer(ANSWER)],
            cmd_handler=lambda cmd: API_EXPLORE if "find /tmp" in cmd
            else (CURL_RESULT if "curl" in cmd else "[exitCode:0]\n"),
            expected_answer="北京",
        )
        brain = Brain()
        brain.llm_calls_today = 3  # 模拟每日额度已耗尽
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.prompts_seen, "额度耗尽也必须发任务 prompt")
        self.assertEqual(brain.llm_calls_today, 3, "任务期 LLM 不应计入每日额度")
        self.assertTrue(sim.submissions)


class TestEngineerDeterministic(unittest.TestCase):
    def test_probe_token_zero_llm(self):
        """工程类：探索→探测→TOKEN 自动提交（全程零 LLM）。"""
        def handler(cmd):
            if "find /tmp/selfEvolutionTask" in cmd:
                return ENG_EXPLORE
            if "-maxdepth 3 -type f -name check" in cmd:
                return ENG_PROBE_OK
            return "[exitCode:0]\n"

        sim = make_sim(tasks=[ENG_TASK], cmd_handler=handler,
                       expected_answer="abc123token")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.submissions)
        self.assertIn("abc123token", sim.submissions[0])
        self.assertEqual(len(sim.prompts_seen), 0, "工程类确定性路径不应调用 LLM")

    def test_crlf_failure_triggers_deterministic_retry(self):
        """工程类：LLM 命令因 CRLF 失败 → 确定性探测重试并拿到 TOKEN。"""
        state = {"probed": False}

        def handler(cmd):
            if "find /tmp/selfEvolutionTask" in cmd:
                return ENG_EXPLORE
            if "-maxdepth 3 -type f -name check" in cmd:
                if not state["probed"]:
                    state["probed"] = True
                    return ENG_PROBE_FAIL
                return ENG_PROBE_OK
            if "sed" in cmd or "check" in cmd:
                return "[exitCode:126]\n/bin/bash: ./check: /bin/sh^M: bad interpreter\n"
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[ENG_TASK],
            llm_script=[llm_cmd("sed -i 's/\\r$//' ./check && ./check")],
            cmd_handler=handler,
            expected_answer="abc123token",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.submissions)
        self.assertIn("abc123token", sim.submissions[0])


class TestSopEvolution(unittest.TestCase):
    def test_sop_extracted_after_completion(self):
        """完成任务后提取 SOP（供第二天复用）。"""
        sim = make_sim(
            tasks=[API_TASK],
            llm_script=[llm_answer(ANSWER)],
            cmd_handler=lambda cmd: API_EXPLORE if "find /tmp" in cmd else "[exitCode:0]\n",
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(brain.task_planner.sop, "应提取 SOP")


class TestAcceptFailNoRetry(unittest.TestCase):
    def test_accept_fail_never_retry(self):
        """acceptTask FAIL（errorCode 4 红线）→ 立即放弃，绝不再试。"""
        sim = make_sim(tasks=[API_TASK], accept_fails=True)
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
