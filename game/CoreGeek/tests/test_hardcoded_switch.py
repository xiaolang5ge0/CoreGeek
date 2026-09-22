"""硬编码能力总开关（config.HARDCODED_ASSIST）回归。

默认关闭：训练期只用通用能力（探索 + LLM），禁用所有硬编码确定性路径。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.task import (
    ST_API_PROBE, ST_LLM, ST_PROBE, TaskPlanner, TaskSession,
)


API_EXPLORE = (
    "[exitCode:0]\n__FILE:/tmp/x/task_1_beijing.md\n=== TASK ===\n查询北京文化遗产\n"
    "=== FILE:/tmp/x/API_DOCS.md ===\nbase http://localhost:8899\nGET /api/v1/heritage/search\n"
    "__DIR:/tmp/x\n"
)
ENG_EXPLORE = (
    "[exitCode:0]\n__FILE:/tmp/x/task_1_alpha.md\n=== TASK ===\n修复 ws_1 通过 ./check\n"
    "=== FILE:/tmp/x/ws_1/spec.md ===\n目录 logs/alpha 权限 755\n__DIR:/tmp/x\n"
)


class TestHardcodedSwitch(unittest.TestCase):
    def setUp(self):
        self._saved = config.HARDCODED_ASSIST

    def tearDown(self):
        config.HARDCODED_ASSIST = self._saved

    def test_flag_toggleable(self):
        # 开关可切换（默认值见 config；PK 期默认开，训练期可设 COREGEEK_HARDCODED=0）
        self.assertIsInstance(config.HARDCODED_ASSIST, bool)

    def test_off_api_goes_to_llm(self):
        config.HARDCODED_ASSIST = False
        p = TaskPlanner()
        s = TaskSession()
        s.task_text = "请阅读task_1_beijing.md，获取任务信息"
        s.target_name = "task_1_beijing.md"
        p._on_explore(s, API_EXPLORE)
        self.assertEqual(s.stage, ST_LLM, "关闭时 API 类不得走硬编码探测")

    def test_off_engineer_goes_to_llm(self):
        config.HARDCODED_ASSIST = False
        p = TaskPlanner()
        s = TaskSession()
        s.task_text = "修复 ws_1"
        p._on_explore(s, ENG_EXPLORE)
        self.assertEqual(s.stage, ST_LLM, "关闭时工程类不得走硬编码探测")

    def test_off_token_not_auto_submitted(self):
        config.HARDCODED_ASSIST = False
        p = TaskPlanner()
        s = TaskSession()
        s.stage = ST_LLM
        p._on_cmd_result(s, "x", "[exitCode:0]\nTOKEN: fc1e78eb2a5a\n")
        # 关闭时默认分支不得自动提交 TOKEN
        self.assertIsNone(s.answer)
        self.assertNotEqual(s.stage, "SUBMIT_ANSWER")

    def test_off_no_build_fix(self):
        config.HARDCODED_ASSIST = False
        p = TaskPlanner()
        s = TaskSession()
        s.task_dir = "/tmp/x/ws_1"
        self.assertIsNone(p._build_fix(s, "[FAIL] DIR logs/alpha → 期望 exists,755，实际 不存在"))

    def test_on_routes_to_probe(self):
        config.HARDCODED_ASSIST = True
        p = TaskPlanner()
        s = TaskSession()
        s.task_text = "请阅读task_1_beijing.md"
        s.target_name = "task_1_beijing.md"
        p._on_explore(s, API_EXPLORE)
        self.assertEqual(s.stage, ST_API_PROBE, "开启时 API 类走硬编码探测")
        s2 = TaskSession()
        s2.task_text = "修复 ws_1"
        p._on_explore(s2, ENG_EXPLORE)
        self.assertEqual(s2.stage, ST_PROBE, "开启时工程类走硬编码探测")


if __name__ == "__main__":
    unittest.main()
