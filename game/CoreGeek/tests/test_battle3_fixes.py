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

TASK = {
    "pos": (14, 14),
    "text": "任务：查询南京文化遗产数据",
    "scoreReward": 50,
    "goldReward": 30,
    "timeoutRounds": 15,
}
API_LOCATE_RESULT = (
    "[exitCode:0]\n__FILE:/data/task_api.md\n__DIR:/data\n"
    "__DOC:/data/task_api.md\nAPI 文档：http://localhost:8899/api 查询文化遗产\n__END"
)


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


class TestApiHarvestDirectAnswer(unittest.TestCase):
    def test_harvest_composes_answer_zero_llm(self):
        """HARVEST 直接合成 __ANSWER（city+total_count）→ 零 LLM 提交（对齐参考文档 #14）。"""
        def handler(cmd):
            if "__FILE" in cmd:
                return API_LOCATE_RESULT
            if "python3 -c" in cmd or "python -c" in cmd:
                return (
                    '[exitCode:0]\n__API status=OK base=http://localhost:8899 path=/api '
                    'auth=bearer param=location city=南京 records=12 total=12\n'
                    '__API_KEYS ["name","type"]\n'
                    '__ANSWER {"city": "南京", "total_count": 12, "world_heritage_count": 2}\n'
                    '__ANSWER_CANDIDATE [{"name":"x","type":"y"}]'
                )
            return "[exitCode:0]\n"

        sim = make_sim(tasks=[TASK], cmd_handler=handler, expected_answer="南京")
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        self.assertGreaterEqual(sim.score, 50)
        self.assertEqual(sim.prompts_seen, [], "HARVEST 直采应零 LLM")
        self.assertTrue(any("南京" in s for s in sim.submissions))


class TestNightMiningNotRecalled(unittest.TestCase):
    def test_workers_mine_at_night_when_safe(self):
        """夜1 机器人远离矿区：工人应继续采集，不群体召回震荡（实战 65 回合 CRITICAL 的反面）。"""
        sim = make_sim(mines={(8, 20): "copper", (6, 22): "stone"})
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 夜间补满矿（Day1 已采空），机器人放到东侧远角（离西部矿区 >15，安全）
        sim.add_mine((8, 20), "copper", remaining=30)
        sim.add_mine((6, 22), "stone", remaining=30)
        for i, (x, y) in enumerate([(30, 5), (31, 6)]):
            sim.spawn_robot(x, y, "smallRobot", hp=40, rid=30700 + i)
        collect_rounds = 0
        crit_rounds = 0
        for _ in range(10):
            response, trace = brain.decide(sim.payload())
            cmds = response["roleCommandMap"]
            if any(c.get("action") == "collect" for c in cmds.values()):
                collect_rounds += 1
            states = (trace.get("workers") or {})
            if any((v or {}).get("state") == "CRITICAL_DEFENSE" for v in states.values()):
                crit_rounds += 1
            sim.apply(response)
            sim.advance()
        self.assertGreater(collect_rounds, 3, "安全时夜间应持续采集")
        self.assertLess(crit_rounds, 5, "不得群体召回震荡")


class TestStockMissionNoCrash(unittest.TestCase):
    def test_stock_mission_trace_serializable(self):
        """Day3+ 备货 WallFixer 的 stock 任务：target 为 None/券名为 str，trace 不得崩溃（异常=封号红线）。"""
        import json as _json
        sim = make_sim()
        brain = Brain()
        run_rounds(brain, sim, DAY1)
        # 推进到 Day3、给足金币、清空 WallFixer → 触发 stock 备货任务
        sim.round_no = 271
        sim.gold = 300
        for rid in (W1, W2):
            sim.role(rid)["backpack"] = [b for b in sim.role(rid)["backpack"] if b != "WallFixer"]
        crashed = False
        for _ in range(15):
            try:
                response, trace = brain.decide(sim.payload())
                _json.dumps(trace, ensure_ascii=False)  # trace 必须可序列化（遥测前提）
            except Exception:
                crashed = True
                break
            sim.apply(response)
            sim.advance()
        self.assertFalse(crashed, "stock 任务不得让 decide/trace 崩溃")


class TestWorldNewsCapture(unittest.TestCase):
    def test_news_saved_each_change(self):
        """用户要求#6：每回合存档 worldNews（去重），供后续宝藏推断。"""
        import json as _json
        base = _json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
        brain = Brain()
        # 第1回合：注入官方消息+民间传闻
        base["worldNews"] = {"officialNews": "铁矿塌方明日停工", "folkLegends": "西部有石门，门需三钥"}
        brain.decide(base)
        self.assertEqual(len(brain.news_log), 1)
        self.assertIn("铁矿塌方", brain.news_log[0]["official"])
        self.assertIn("三钥", brain.news_log[0]["folk"])
        # 第2回合：新闻不变 → 去重不重复存
        base["roundNo"] = 2
        brain.decide(base)
        self.assertEqual(len(brain.news_log), 1)
        # 第3回合：新闻变化 → 追加
        base["roundNo"] = 3
        base["worldNews"] = {"officialNews": "铜价上涨", "folkLegends": "圆圈里三道杠"}
        brain.decide(base)
        self.assertEqual(len(brain.news_log), 2)
        self.assertIn("铜价上涨", brain.news_log[1]["official"])


if __name__ == "__main__":
    unittest.main()
