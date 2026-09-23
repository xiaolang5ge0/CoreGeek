"""issue IKI8DZ 重构回归：自进化任务改为 LLM 驱动（移除 HARVEST + 上下文/经验）。

覆盖：
- task_type 分类（api / engineering / general，关键词适度扩充不误判）。
- transcript 长度控制（30k 规则：前 12k + 后 18k + 省略标记）。
- local_answer 确定性直答（显式答案字段 / 简单算术 / 占位符拒绝）。
- learn_family_notes（从 4xx 学习认证/参数教训，按 task_type 固化）。
- 分页完整性提示（去重记录 < total_count → 提示 LLM 继续取）。
- recompute_api_answer（记录取全后用 transcript 重算关键字段）。
- FINAL_ANSWER 提取；prompt 必带 transcript + 家族经验。
- HARVEST 模块已移除。
"""
import importlib.util
import json
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.task import (
    ST_LLM, ST_SUBMIT, TRANSCRIPT_OMIT, TaskPlanner, TaskSession, _classify,
)

config.HARDCODED_ASSIST = True


class TestTaskTypeClassification(unittest.TestCase):
    def test_api_signals(self):
        self.assertEqual(_classify("base http://localhost:8899 GET /api/v1/heritage/search"), "api")
        self.assertEqual(_classify("curl -H 'X-API-Key: k' http://127.0.0.1:8899/x"), "api")
        self.assertEqual(_classify("见 API_DOCS.md，用 Authorization: Bearer <key>"), "api")

    def test_engineering_signals(self):
        self.assertEqual(_classify("修复 ws_1，通过 ./check"), "engineering")
        self.assertEqual(_classify("按 spec.md 修复，check 输出 [FAIL]/[PASS]"), "engineering")

    def test_general_fallback(self):
        self.assertEqual(_classify("请统计文件里出现次数最多的词"), "general")
        self.assertEqual(_classify(""), "general")

    def test_api_token_not_misclassified_as_engineering(self):
        # API 的 Bearer token 含 token 字样，不得因此判成工程类
        self.assertEqual(_classify("Bearer token: abc; /api/ records"), "api")

    def test_path_without_api_slash_still_api(self):
        # §4.3 盲区：服务路径是 /heritage 而非 /api/heritage → 仍应判 api
        self.assertEqual(_classify("GET http://10.0.0.5:8899/heritage/search?city=北京"), "api")


class TestTranscriptTruncation(unittest.TestCase):
    def test_short_transcript_kept(self):
        p = TaskPlanner()
        s = TaskSession()
        s.transcript = ["COMMAND: a\nRESULT: b", "COMMAND: c\nRESULT: d"]
        text = p._transcript_text(s)
        self.assertIn("COMMAND: a", text)
        self.assertNotIn(TRANSCRIPT_OMIT, text)

    def test_long_transcript_head_and_tail(self):
        p = TaskPlanner()
        s = TaskSession()
        s.transcript = ["H" * 20000, "T" * 20000]
        text = p._transcript_text(s)
        self.assertIn(TRANSCRIPT_OMIT, text)
        self.assertEqual(len(text), 12000 + len(TRANSCRIPT_OMIT) + 18000)
        self.assertTrue(text.startswith("H"))
        self.assertTrue(text.endswith("T"))


class TestLocalAnswer(unittest.TestCase):
    def test_explicit_answer_field(self):
        self.assertEqual(TaskPlanner()._local_answer("答案是：42"), "42")
        self.assertEqual(TaskPlanner()._local_answer("answer: hello"), "hello")

    def test_arithmetic(self):
        self.assertEqual(TaskPlanner()._local_answer("请计算 3+4*2"), "11")
        self.assertEqual(TaskPlanner()._local_answer("计算：(10-4)/2"), "3.0")

    def test_placeholder_rejected(self):
        self.assertIsNone(TaskPlanner()._local_answer("答案：xxx"))
        self.assertIsNone(TaskPlanner()._local_answer("请阅读task_1_beijing.md，获取任务信息"))

    def test_unsafe_expression_rejected(self):
        self.assertIsNone(TaskPlanner()._local_answer("计算 __import__('os').system('x')"))


class TestFamilyNotes(unittest.TestCase):
    def test_learns_from_4xx(self):
        p = TaskPlanner()
        s = TaskSession()
        s.task_type = "api"
        p._learn_notes(s, '{"error":"expected format: Authorization: Bearer <key>"}')
        p._learn_notes(s, '{"error":"missing required parameter: location"}')
        p._learn_notes(s, '{"error":"unknown parameter: pageSize"}')
        notes = "\n".join(p.notes["api"])
        self.assertIn("Bearer", notes)
        self.assertIn("location", notes)
        self.assertIn("pageSize", notes)

    def test_notes_are_deduped_and_type_keyed(self):
        p = TaskPlanner()
        s = TaskSession()
        s.task_type = "api"
        p._learn_notes(s, "missing required parameter: location")
        p._learn_notes(s, "missing required parameter: location")
        self.assertEqual(len(p.notes["api"]), 1)
        self.assertNotIn("engineering", p.notes)


class TestPaginationNote(unittest.TestCase):
    def _session_with(self, records, total):
        s = TaskSession()
        s.task_type = "api"
        payload = {"code": 200, "data": {"records": records, "pagination": {"total_count": total}}}
        s.transcript = [f"COMMAND: curl x\nRESULT: {json.dumps(payload, ensure_ascii=False)}"]
        return s

    def test_incomplete_flags_llm(self):
        p = TaskPlanner()
        s = self._session_with([{"id": 1}, {"id": 2}], 5)
        note = p._pagination_note(s)
        self.assertIn("分页不完整", note)
        self.assertIn("5", note)

    def test_complete_no_note(self):
        p = TaskPlanner()
        s = self._session_with([{"id": 1}, {"id": 2}], 2)
        self.assertEqual(p._pagination_note(s), "")

    def test_duplicate_records_deduped(self):
        p = TaskPlanner()
        s = self._session_with([{"id": 1}, {"id": 1}], 2)
        note = p._pagination_note(s)
        self.assertIn("分页不完整", note, "重复记录应按 id 去重后再比对")


class TestRecomputeApiAnswer(unittest.TestCase):
    def _session_with(self, records, total):
        s = TaskSession()
        s.task_type = "api"
        payload = {"code": 200, "data": {"records": records, "pagination": {"total_count": total}}}
        s.transcript = [f"COMMAND: curl x\nRESULT: {json.dumps(payload, ensure_ascii=False)}"]
        return s

    def test_recomputes_when_complete(self):
        p = TaskPlanner()
        s = self._session_with(
            [{"id": 1, "type": "宫殿", "protected_level": "世界遗产"},
             {"id": 2, "type": "园林", "protected_level": "省级"}],
            2,
        )
        out = json.loads(p._recompute_api_answer(
            s, '{"total_count": 1, "types": ["宫殿"], "world_heritage_count": 0}'))
        self.assertEqual(out["total_count"], 2)
        self.assertEqual(sorted(out["types"]), ["园林", "宫殿"])
        self.assertEqual(out["world_heritage_count"], 1)

    def test_no_recompute_when_incomplete(self):
        p = TaskPlanner()
        s = self._session_with([{"id": 1, "type": "宫殿"}], 5)
        ans = '{"total_count": 1, "types": ["宫殿"]}'
        self.assertEqual(p._recompute_api_answer(s, ans), ans, "记录不全时不得用不完整页面覆盖")

    def test_disabled_when_hardcoded_off(self):
        saved = config.HARDCODED_ASSIST
        config.HARDCODED_ASSIST = False
        try:
            p = TaskPlanner()
            s = self._session_with([{"id": 1, "type": "宫殿"}, {"id": 2, "type": "园林"}], 2)
            ans = '{"total_count": 1, "types": ["宫殿"]}'
            self.assertEqual(p._recompute_api_answer(s, ans), ans)
        finally:
            config.HARDCODED_ASSIST = saved


class TestFinalAnswerExtraction(unittest.TestCase):
    def test_final_answer_submitted(self):
        p = TaskPlanner()
        s = TaskSession()
        ok = p._try_final_answer_submit(s, '[exitCode:0]\nFINAL_ANSWER: {"a": 1}\n')
        self.assertTrue(ok)
        self.assertEqual(s.stage, ST_SUBMIT)
        self.assertIn('"a": 1', s.answer)

    def test_empty_final_answer_rejected(self):
        p = TaskPlanner()
        s = TaskSession()
        ok = p._try_final_answer_submit(s, 'FINAL_ANSWER: {"total_count": 0, "types": []}')
        self.assertFalse(ok)


class TestPromptCarriesContext(unittest.TestCase):
    def test_prompt_has_transcript_notes_and_contract(self):
        p = TaskPlanner()
        p.notes["api"] = ["认证方式：用 Authorization: Bearer <key>"]
        p.api_facts["param"] = "location"
        s = TaskSession()
        s.task_type = "api"
        s.task_text = "查询北京文化遗产"
        s.transcript = ["COMMAND: curl http://localhost:8899/api/x\nRESULT: ok"]
        prompt = p._build_prompt(s)
        self.assertIn("transcript", prompt)
        self.assertIn("curl http://localhost:8899/api/x", prompt)
        self.assertIn("家族经验笔记", prompt)
        self.assertIn("Bearer", prompt)
        self.assertIn("跨任务 API 经验", prompt)
        self.assertIn("cmd", prompt)

    def test_api_task_goes_to_llm_not_harvest(self):
        p = TaskPlanner()
        s = TaskSession()
        s.task_text = "请阅读task_1_beijing.md"
        s.target_name = "task_1_beijing.md"
        p._on_explore(
            s,
            "[exitCode:0]\n__FILE:/tmp/x/task_1_beijing.md\n=== TASK ===\n查询北京文化遗产\n"
            "=== FILE:/tmp/x/API_DOCS.md ===\nbase http://localhost:8899\nGET /api/v1/heritage/search\n"
            "__DIR:/tmp/x\n",
        )
        self.assertEqual(s.stage, ST_LLM)
        self.assertEqual(s.task_type, "api")


class TestHarvestRemoved(unittest.TestCase):
    def test_harvest_module_removed(self):
        self.assertIsNone(
            importlib.util.find_spec("agent.planners._harvest_data"),
            "HARVEST 模块应已移除（issue IKI8DZ）",
        )


if __name__ == "__main__":
    unittest.main()
