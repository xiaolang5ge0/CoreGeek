"""第三轮实战修复回归：Day1冲刺/采集看门狗/夜采安全/遥测加密/任务超时/参数纠错。"""
import base64
import json
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.planners.task import TaskPlanner, TaskSession
from agent.protocol import Pos
from agent.telemetry import compact_record, encrypt_text

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


class TestDay1Rush(unittest.TestCase):
    def test_both_workers_stone_when_available(self):
        """Day1 有石矿：双工人都进石料岗（冲刺建墙），墙数显著多于单工人。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (7, 26): "stone", (8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(len(sim.walls()), 10)

    def test_rush_disabled_without_stone(self):
        """无石矿：冲刺关闭，经济岗正常采铜（不双双饿死）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        brain = Brain()
        history = run_rounds(brain, sim, 30)
        collects = sum(
            1 for resp, _ in history
            for rid in (W1, W2)
            if (cmd_of(resp, rid) or {}).get("action") == "collect"
        )
        self.assertGreater(collects, 5)


class TestCollectWatchdog(unittest.TestCase):
    def test_dead_mine_released(self):
        """矿在 zones 但采不到（剩余0）：3 次 collect 失败后解锁+拉黑，不再死循环。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, 14)  # 工人到位开始采（可能已采空移除）
        sim.add_mine((8, 20), "copper", remaining=0)  # 矿格在 zones 但采空
        sim.role(W2)["backpack"] = []  # 清空背包，排除卖货分支干扰
        brain.worker_fsms[W2].mine = None  # 强制重新锁定到该矿
        history = run_rounds(brain, sim, 8)
        collects = sum(
            1 for resp, _ in history
            if (cmd_of(resp, W2) or {}).get("action") == "collect"
        )
        self.assertLessEqual(collects, 3)
        self.assertTrue(brain.mine_blacklist.get(Pos(8, 20), 0) > 0)


class TestNightMineSafety(unittest.TestCase):
    def test_pick_mine_away_from_robots(self):
        """夜采选矿：机器人在 A 矿旁 → 选 B 矿（用户场景：左上矿安全，左下矿有兵）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(6, 26): "copper", (6, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 机器人在 (6,20) 矿旁刷出
        sim.spawn_robot(6, 20, "middleRobot", hp=60, rid=30501)
        brain.worker_fsms[W1].mine = None
        brain.worker_fsms[W2].mine = None
        response, trace = brain.decide(sim.payload())
        mines_locked = [
            info.get("mine") for info in (trace.get("workers") or {}).values()
        ]
        for m in mines_locked:
            if m:
                self.assertNotEqual((m["x"], m["y"]), (6, 20), "不得锁定机器人旁的矿")


class TestTelemetryEncryption(unittest.TestCase):
    def test_encrypt_roundtrip_and_compact(self):
        import sys
        from pathlib import Path
        tools = Path(__file__).resolve().parents[1] / "tools"
        if str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        from decrypt_log import decrypt_text as dt

        payload = json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
        prev: list = []
        rec = compact_record(85, payload, {"roleCommandMap": {}}, {"day": 1}, prev)
        line = encrypt_text(json.dumps(rec, ensure_ascii=False))
        # 密文不含明文关键字
        self.assertNotIn("roundNo", line)
        self.assertNotIn("station", line)
        # 可解密还原
        restored = json.loads(dt(line))
        self.assertEqual(restored["r"], 85)
        self.assertTrue(restored["u"])  # 角色数组存在


class TestTaskDeadline(unittest.TestCase):
    def test_force_submit_before_timeout(self):
        """任务超时预算（实战 15 回合）：deadline-2 强制提交保底答案。"""
        planner = TaskPlanner()
        session = TaskSession()
        session.task_text = "任务"
        session.started_round = 100
        session.timeout_rounds = 15
        session.stage = "LLM"
        session.best_answer = {"x": 1}
        session.submitted = False

        class FakeTurn:
            phase_task = "任务"
            round_no = 113  # 100+15-2
            last_cmd_result = ""
            llm_resp = ""
            errors = ()

        out = planner.work(FakeTurn(), session)
        self.assertIsNotNone(out.submit)
        self.assertEqual(out.prompt, "")


class TestParamFix(unittest.TestCase):
    def test_missing_param_retry(self):
        """'Missing required parameter: location' → 确定性 curl 重试（不耗 LLM）。"""
        planner = TaskPlanner()
        session = TaskSession()
        session.task_text = "查询南京天气"
        session.task_desc = "API http://localhost:8899"
        session.evidence = [
            ('curl -s -H "Authorization: Bearer k123456" "http://localhost:8899/api/v1/heritage/search?city=南京"',
             '[exitCode:0]\n{"status":"error","message":"Missing required parameter: location","code":400}'),
        ]
        cmd = planner._param_fix_cmd(session)
        self.assertIsNotNone(cmd)
        self.assertIn("location=南京", cmd)
        self.assertIn("Bearer k123456", cmd)


if __name__ == "__main__":
    unittest.main()
