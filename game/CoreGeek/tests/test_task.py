"""P5 验收 v2：任务流 —— 接任务/LOCATE/三类任务处理器/黄昏返程/accept FAIL 不重试。

协议参照《自进化策略.md》：
- LOCATE 确定性定位（__FILE/__DIR/__DOC）→ 分类（工程修复/API/通用LLM）
- LLM 严格单行：CMD: <命令> 或 ANSWER: <JSON>
- acceptTask FAIL 立即放弃不重试（errorCode 4 封号红线）
"""
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

GENERIC_LOCATE_RESULT = (
    "[exitCode:0]\n__FILE:/data/task_1.md\n__DIR:/data\n"
    "__DOC:/data/task_1.md\n任务：查询北京今日天气\n__END"
)

WS_LOCATE_RESULT = (
    "[exitCode:0]\n__FILE:/tmp/selfEvolutionTask/ws_3/task_ws3.md\n"
    "__DIR:/tmp/selfEvolutionTask/ws_3\n"
    "__DOC:/tmp/selfEvolutionTask/ws_3/task_ws3.md\n"
    "任务：修复 ws_3 工程，通过 ./check\n__END"
)

API_LOCATE_RESULT = (
    "[exitCode:0]\n__FILE:/data/task_api.md\n__DIR:/data\n"
    "__DOC:/data/task_api.md\nAPI 文档：http://localhost:8080/weather 查询天气\n__END"
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


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
    base.update(kw)
    return SimWorld(**base)


class TestGenericLLMTask(unittest.TestCase):
    def test_day1_task_completed(self):
        """通用任务：LOCATE → LLM 一次出 ANSWER → 提交成功 → 黄昏前回 CP。"""
        sim = make_sim(
            tasks=[TASK],
            llm_script=['ANSWER: {"city": "北京", "weather": "晴"}'],
            cmd_handler=lambda cmd: GENERIC_LOCATE_RESULT if "__FILE" in cmd else "[exitCode:0]\n",
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 50)  # 任务积分到账（金币可能已被升级花掉）
        self.assertTrue(sim.submissions)
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)

    def test_explore_then_answer(self):
        """LLM 给 CMD → 沙盒执行 → 再给 ANSWER → 提交。"""
        sim = make_sim(
            tasks=[TASK],
            llm_script=["CMD: ls /data", 'ANSWER: {"city": "北京"}'],
            cmd_handler=lambda cmd: (
                GENERIC_LOCATE_RESULT if "__FILE" in cmd else "[exitCode:0]\n北京 晴 25C"
            ),
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertIn("ls /data", sim.cmds_seen)
        self.assertTrue(sim.submissions)
        self.assertGreaterEqual(sim.score, 50)


class TestWsTask(unittest.TestCase):
    def test_ws_fix_zero_llm(self):
        """工程修复类：LOCATE → 探测 → 确定性 sed/mkdir 修复 → TOKEN → 提交，全程零 LLM。"""
        def ws_handler(cmd):
            if "__FILE" in cmd:
                return WS_LOCATE_RESULT
            if "find . -maxdepth" in cmd:
                return (
                    "[exitCode:0]\n.\n./check\n./spec.md\n__SPEC__\n目录 data 权限 755\n"
                    "__CHECK__\n[FAIL] DIR data — 期望 exists,755"
                )
            if "mkdir" in cmd:
                return "[exitCode:0]\n[PASS] all checks passed\nTOKEN: fc1e78eb2a5a"
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[TASK],
            cmd_handler=ws_handler,
            expected_answer="fc1e78eb2a5a",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 50)
        self.assertTrue(any("fc1e78eb2a5a" in s for s in sim.submissions))
        self.assertEqual(sim.prompts_seen, [])  # 确定性修复零 LLM


class TestApiTask(unittest.TestCase):
    def test_api_harvest_then_answer(self):
        """API 类：LOCATE → harvest 探测 → LLM 组答 → 提交。"""
        def api_handler(cmd):
            if "__FILE" in cmd:
                return API_LOCATE_RESULT
            if "python3 -c" in cmd or "python -c" in cmd:
                return (
                    "[exitCode:0]\n__API status=OK base=http://localhost:8080 path=/weather "
                    "auth=none records=3 total=3\n"
                    '__ANSWER_CANDIDATE [{"city":"北京","weather":"晴"}]'
                )
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[TASK],
            llm_script=['ANSWER: {"city": "北京", "weather": "晴"}'],
            cmd_handler=api_handler,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 50)
        self.assertTrue(sim.submissions)


class TestWsRealSpecFormat(unittest.TestCase):
    def test_real_spec_format(self):
        """真实 spec 格式：'- logs/alpha/ 必须存在，权限为 755' + '第 3 行：`port 8080`'。"""
        def handler(cmd):
            if "__FILE" in cmd:
                return WS_LOCATE_RESULT
            if "find . -maxdepth" in cmd:
                return (
                    "[exitCode:0]\n.\n./check\n./spec.md\n__SPEC__\n"
                    "# 规范 alpha\n- logs/alpha/ 必须存在，权限为 755\n"
                    "## 配置文件 config/alpha.conf\n- 第 3 行：`port 8080`\n"
                    "__CHECK__\n[FAIL] 3/6\n"
                )
            if "mkdir" in cmd or "sed -i" in cmd:
                return "[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fc1e78eb2a5a"
            return "[exitCode:0]\n"

        sim = make_sim(tasks=[TASK], cmd_handler=handler, expected_answer="fc1e78eb2a5a")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 50)
        self.assertEqual(sim.prompts_seen, [])  # 确定性修复零 LLM
        fix_cmds = [c for c in sim.cmds_seen if "mkdir" in c or "sed" in c]
        self.assertTrue(any("logs/alpha" in c for c in fix_cmds))


class TestApiAuthRetry(unittest.TestCase):
    def test_bearer_retry(self):
        """服务端要 Authorization: Bearer → 规划器确定性重发（不靠 LLM 试错）。"""
        def handler(cmd):
            if "__FILE" in cmd:
                return API_LOCATE_RESULT
            if "python3 -c" in cmd or "python -c" in cmd:
                return "[exitCode:0]\n__API status=FAIL base=http://localhost:8899 paths=2 keys=2"
            if "curl" in cmd and "X-API-Key" in cmd:
                return (
                    '[exitCode:0]\n{"status":"error","message":'
                    '"Authentication failed: Missing \'Authorization\' header. Expected format: Bearer"}'
                )
            if "Authorization: Bearer" in cmd:
                return '[exitCode:0]\n[{"city":"北京","weather":"晴"}]'
            return "[exitCode:0]\n"

        sim = make_sim(
            tasks=[TASK],
            llm_script=[
                'CMD: curl -s -H "X-API-Key: heritage-api-key-2024" "http://localhost:8899/api/v1/heritage/search?city=北京"',
                'ANSWER: {"city": "北京", "weather": "晴"}',
            ],
            cmd_handler=handler,
            expected_answer="北京",
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        bearer_cmds = [c for c in sim.cmds_seen if "Authorization: Bearer" in c]
        self.assertTrue(bearer_cmds, "应确定性重发 Bearer 请求")
        self.assertIn("heritage-api-key-2024", bearer_cmds[0])


class TestAcceptFailNoRetry(unittest.TestCase):
    def test_accept_fail_never_retry(self):
        """acceptTask FAIL（errorCode 4 红线）→ 立即放弃，绝不再试。"""
        sim = make_sim(tasks=[TASK], accept_fails=True)
        brain = Brain()
        run_rounds(brain, sim, 30)
        self.assertEqual(sim.accept_count, 1)  # 只尝试过一次


class TestDuskReturn(unittest.TestCase):
    def test_abort_task_before_night(self):
        """任务做不完也必须入夜前回家（离开任务点=任务结束）。"""
        sim = make_sim(
            tasks=[TASK],
            llm_script=["CMD: true"] * 100,  # 永远探索不完
            cmd_handler=lambda cmd: (
                GENERIC_LOCATE_RESULT if "__FILE" in cmd else "[exitCode:0]\n"
            ),
        )
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        cp = brain.layout.control_point
        pos = sim.role(PIONEER)["pos"]
        self.assertEqual(Pos(pos["x"], pos["y"]), cp)
        self.assertEqual(sim.phase_task, "")


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
