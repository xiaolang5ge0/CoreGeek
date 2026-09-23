"""自进化任务端到端验证（issue#28–#32 交接结论 → 当前代码能否通过）。

模拟沙盒（cmd_handler）覆盖：健壮探索 → 工程探测/API 探测 → 提交。
验证点（对照交接文档 §5 验收点）：
1. API 任务：出现读取 API_DOCS 的探索 + 确定性探测命中可用组合 → 提交。
2. 工程任务：读 spec/check 拿到真实 TOKEN 后提交；**禁止**提交任务书示例占位 token。
3. 答案安全校验：全零/错误答案不提交。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from agent import config

# 本模块测试"硬编码确定性能力"，显式开启开关（默认关闭见 test_hardcoded_switch.py）
config.HARDCODED_ASSIST = True

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Turn

PIONEER = 10011
DAY1 = 70

API_TASK = {"pos": (14, 14), "text": "请阅读task_1_beijing.md，获取任务信息",
            "scoreReward": 80, "goldReward": 80, "timeoutRounds": 15}
ENG_TASK = {"pos": (14, 14), "text": "任务：修复 ws_3 工程，通过 ./check",
            "scoreReward": 50, "goldReward": 30, "timeoutRounds": 20}

# 探索输出：含任务书 + 二级文档（API_DOCS / spec）
API_EXPLORE = (
    "[exitCode:0]\n"
    "__FILE:/tmp/selfEvolutionTask/1-unknown-api/task_1_beijing.md\n"
    "=== TASK ===\n# 自进化任务 A-1：查询北京文化遗产\n提交格式 {\"token\": \"xxx\"}\n"
    "=== FILE:/tmp/selfEvolutionTask/1-unknown-api/API_DOCS.md ===\n"
    "base http://localhost:8899\nGET /api/v1/heritage/search\nAPI Key: `heritage-api-key-2024`\n"
    "__DIR:/tmp/selfEvolutionTask/1-unknown-api\n"
)
ENG_EXPLORE = (
    "[exitCode:0]\n"
    "__FILE:/tmp/selfEvolutionTask/2-engineering-fix/task_1_alpha.md\n"
    "=== TASK ===\n# 自进化任务 B-1：修复应用 alpha 部署\n提交格式 {\"token\": \"xxx\"}\n"
    "=== FILE:/tmp/selfEvolutionTask/2-engineering-fix/ws_1/spec.md ===\n"
    "目录 logs/alpha 权限 755\n"
    "__DIR:/tmp/selfEvolutionTask/2-engineering-fix\n"
)
API_RECORDS = (
    '[exitCode:0]\n{"code":200,"data":{"records":['
    '{"id":1,"name":"故宫","type":"古建筑"},'
    '{"id":2,"name":"周口店遗址","type":"古遗址"}],'
    '"pagination":{"total_count":2,"offset":0,"limit":10}}}\n'
)
ENG_PROBE_FAIL = (
    "[exitCode:0]\n"
    "=== CHECK ===\n"
    "[FAIL] 3/6 通过，3 失败\n"
    "[FAIL] DIR   logs/alpha   → 期望 exists,755，实际 不存在\n"
    "[FAIL] LINE  config/alpha.conf:3   → 期望 port 8080，实际 port 9999\n"
    "[FAIL] LINE  config/alpha.conf:6   → 期望 name alpha-app，实际 name wrong-app\n"
)
ENG_PROBE_OK = "[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fc1e78eb2a5a\n"
API_ANSWER = '{"city":"北京","total_count":3,"world_heritage_count":1,"types":["古建筑","古遗址"]}'


def llm_cmd(cmd):
    return json.dumps({"cmd": cmd, "answer": "", "isFinished": False}, ensure_ascii=False)


def llm_answer(ans):
    return json.dumps({"cmd": "", "answer": ans, "isFinished": True}, ensure_ascii=False)


def run_rounds(brain, sim, n):
    for _ in range(n):
        response, _ = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()


class TestApiTaskE2E(unittest.TestCase):
    def test_api_task_llm_curl_then_submit(self):
        """API 类交给 LLM（issue IKI8DZ）：探索 → LLM 给 curl → 结果入 transcript → LLM 给答案 → 提交。"""
        captured = []

        def handler(cmd):
            captured.append(cmd)
            if "find /tmp/selfEvolutionTask" in cmd:
                return API_EXPLORE
            if "curl" in cmd:
                return API_RECORDS
            return "[exitCode:0]\n"

        sim = SimWorld(
            station_pos=(10, 24), mines={(6, 22): "stone"},
            tasks=[API_TASK], cmd_handler=handler, expected_answer="北京",
            llm_script=[
                llm_cmd("curl 'http://localhost:8899/api/v1/heritage/search?location=北京'"),
                llm_answer('{"city":"北京","total_count":2,"types":["古建筑","古遗址"]}'),
            ],
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(sim.prompts_seen, "API 类应走 LLM（不再有确定性收割脚本）")
        self.assertFalse(any("base64" in c for c in captured), "不得再执行 HARVEST 收割脚本")
        self.assertTrue(any("curl" in c for c in captured), "应执行 LLM 给的 curl 命令")
        self.assertTrue(sim.submissions, "应提交答案")
        self.assertIn("北京", sim.submissions[0])
        self.assertGreaterEqual(sim.score, 80)


class TestEngineerTaskE2E(unittest.TestCase):
    def test_engineer_reads_check_token_not_placeholder(self):
        captured = []

        def handler(cmd):
            captured.append(cmd)
            if "find /tmp/selfEvolutionTask" in cmd:
                return ENG_EXPLORE
            if "-maxdepth 3 -type f -name check" in cmd:   # 工程确定性探测
                return ENG_PROBE_OK
            return "[exitCode:0]\n"

        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"},
                       tasks=[ENG_TASK], cmd_handler=handler,
                       expected_answer="fc1e78eb2a5a")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertTrue(any("-name check" in c for c in captured), "应触发工程 check 探测")
        self.assertTrue(sim.submissions, "应提交 token")
        self.assertIn("fc1e78eb2a5a", sim.submissions[0])
        self.assertNotIn("xxx", sim.submissions[0])

    def test_engineer_deterministic_fix_zero_llm(self):
        """工程类：探测拿 FAIL → 确定性修复(mkdir/chmod/sed) → check 通过 → 提交，零 LLM。"""
        captured = []

        def handler(cmd):
            captured.append(cmd)
            if "find /tmp/selfEvolutionTask" in cmd:
                return ENG_EXPLORE
            if "-maxdepth 3 -type f -name check" in cmd:
                return ENG_PROBE_FAIL           # 首次探测：check 有 3 处 FAIL
            if "mkdir" in cmd or "sed -i" in cmd:
                return ENG_PROBE_OK             # 修复后再 check：通过 + TOKEN
            return "[exitCode:0]\n"

        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"},
                       tasks=[ENG_TASK], cmd_handler=handler,
                       expected_answer="fc1e78eb2a5a")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        fix = next((c for c in captured if "logs/alpha" in c or "port 8080" in c), None)
        self.assertIsNotNone(fix, "应生成确定性修复命令")
        self.assertIn("logs/alpha", fix)
        self.assertIn("port 8080", fix)
        self.assertIn("name alpha-app", fix)
        self.assertTrue(sim.submissions)
        self.assertIn("fc1e78eb2a5a", sim.submissions[0])
        self.assertEqual(len(sim.prompts_seen), 0, "工程类应零 LLM")


class TestRefineRetry(unittest.TestCase):
    def test_error_code_2_pulls_back_to_llm(self):
        """IKHYQC/IKHYQB 根因：提交被判错(errorCode=2)后必须回 LLM 重试，不能停在 DONE。"""
        from agent.planners.task import ST_DONE, ST_LLM, ST_WAIT_LLM
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone"}, tasks=[API_TASK])
        sim.phase_task = API_TASK["text"]
        sim.next_errors = [{"errorCode": 2, "description": "键值比对不通过: $/types: 数组长度 9 != 7"}]
        brain = Brain()
        planner = brain.task_planner
        session = brain.task_session
        session.task_text = API_TASK["text"]
        session.stage = ST_DONE          # 已提交 → DONE
        session.answer = '{"types": []}'
        turn = Turn.load(sim.payload())
        planner.work(turn, session)
        self.assertIn(session.stage, (ST_LLM, ST_WAIT_LLM), "判错后应回到 LLM 重试")
        self.assertIsNone(session.answer)
        self.assertIn("types", session.last_error)


class TestAnswerSafety(unittest.TestCase):
    def test_all_zero_answer_rejected(self):
        from agent.planners.task import TaskPlanner, TaskSession, ST_SUBMIT
        planner = TaskPlanner()
        s = TaskSession()
        planner._on_llm_result(s, llm_answer('{"total_count":0,"world_heritage_count":0,"types":[]}'))
        self.assertNotEqual(s.stage, ST_SUBMIT, "全零答案不得提交")

    def test_error_answer_rejected(self):
        from agent.planners.task import TaskPlanner, TaskSession, ST_SUBMIT
        planner = TaskPlanner()
        s = TaskSession()
        planner._on_llm_result(s, llm_answer('{"error":"401 unauthorized"}'))
        self.assertNotEqual(s.stage, ST_SUBMIT, "错误答案不得提交")

    def test_valid_answer_accepted(self):
        from agent.planners.task import TaskPlanner, TaskSession, ST_SUBMIT
        planner = TaskPlanner()
        s = TaskSession()
        planner._on_llm_result(s, llm_answer(API_ANSWER))
        self.assertEqual(s.stage, ST_SUBMIT)


if __name__ == "__main__":
    unittest.main()
