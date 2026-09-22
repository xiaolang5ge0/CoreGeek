"""全局集成回归：完整多日流程（建墙→任务→夜防→升级）0 崩溃、基地存活、任务确定性完成。

裁剪为 2 天(260回合)以平衡覆盖与测试速度；完整 10 天验证见 tools 级仿真。
"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain

TASK = {"pos": (14, 14), "text": "请阅读task_1_alpha.md", "scoreReward": 80,
        "goldReward": 80, "timeoutRounds": 15}
WS_LOCATE = ("[exitCode:0]\n__FILE:/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix/task_1_alpha.md\n"
             "__DIR:/tmp/selfEvolutionTask/1-fixed-step/2-engineering-fix\n"
             "__DOC:.../task_1_alpha.md\n任务：修复 ws_1 工程，通过 ./check\n__END")


def _handler(cmd):
    if "__FILE" in cmd:
        return WS_LOCATE
    if "find . -maxdepth" in cmd:
        return ("[exitCode:0]\n.\n./check\n./spec.md\n__SPEC__\n# 规范 alpha\n"
                "- logs/alpha/ 必须存在，权限为 755\n## 配置文件 config/alpha.conf\n"
                "- 第 3 行：`port 8080`\n__CHECK__\n[FAIL] DIR logs/alpha 期望 exists,755 实际 不存在")
    if "mkdir" in cmd or "sed -i" in cmd:
        return "[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fc1e78eb2a5a"
    return "[exitCode:0]\n"


class TestFullGameIntegration(unittest.TestCase):
    def test_two_day_full_pipeline(self):
        sim = SimWorld(
            station_pos=(10, 24),
            mines={(6, 22): "stone", (7, 26): "stone", (8, 20): "copper", (14, 6): "iron"},
            tasks=[TASK], cmd_handler=_handler, expected_answer="fc1e78eb2a5a",
        )
        brain = Brain()
        respawns = [(8, 20, "copper"), (6, 22, "stone"), (16, 14, "copper"), (7, 26, "stone")]
        crashes = 0
        for r in range(1, 261):
            if not sim.mines and respawns:
                x, y, k = respawns.pop(0)
                sim.add_mine((x, y), k)
            if r % 130 == 71:  # 夜潮
                day = (r - 1) // 130 + 1
                for i in range(50 + day * 8):
                    sim.spawn_robot(16 + i % 6, 19 + i % 8, "smallRobot", hp=40, rid=30000 + i)
            try:
                resp, trace = brain.decide(sim.payload())
                sim.apply(resp)
                sim.advance()
            except Exception:
                crashes += 1
                sim.advance()
        # 核心断言
        self.assertEqual(crashes, 0, "全流程不得崩溃（异常=封号红线）")
        self.assertTrue(sim.base_alive, "基地必须存活")
        self.assertGreaterEqual(len(sim.walls()), 10, "Day1 应建成≥10 墙")
        self.assertGreaterEqual(len(sim.submissions), 2, "任务应确定性完成多次")
        self.assertEqual(sim.prompts_seen, [], "工程类任务应零 LLM")
        self.assertGreaterEqual(sim.score, 160, "任务积分应到账")
        # 出生点遥测已记录（供后续 FRONT 修正/夜间避让）
        self.assertGreaterEqual(len(brain.robot_spawn_log), 1)


if __name__ == "__main__":
    unittest.main()
