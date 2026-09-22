"""IKHYSK/IKHYTB 回归：开拓者归位粘性 / 工人夜间不回防 / 墙券限购6 / 新闻时间词 / 矿点含卖货路程。"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
config.HARDCODED_ASSIST = False

from harness import SimWorld
from agent.brain import Brain
from agent.fsm_pioneer import PioneerFSM, STATE_TASK_TRAVEL
from agent.fsm_worker import WorkerFSM, ROLE_REPAIRER
from agent.planners.news import NewsEconomy
from agent.planners.upgrade import WALL_VOUCHER_BATCH
from agent.protocol import Pos, Turn

W1, W2, PIONEER = 10010, 10012, 10011
DAY1 = 70


def make_sim(**kw):
    base = dict(station_pos=(10, 24), mines={})
    base.update(kw)
    return SimWorld(**base)


class TestMinerNoNightReturn(unittest.TestCase):
    def test_miner_keeps_mining_when_robots_cleared(self):
        """IKHYTB：夜间机器人清空后不得再触发返程死线（应继续采矿）。"""
        sim = make_sim(mines={(6, 22): "stone", (8, 20): "copper"})
        sim.add_mine((6, 22), "stone", remaining=60)
        sim.add_mine((8, 20), "copper", remaining=60)
        brain = Brain()
        for _ in range(DAY1):
            r, _ = brain.decide(sim.payload())
            sim.apply(r)
            sim.advance()
        # 进入夜间、无机器人、接近天亮（返程死线本会触发）
        sim.round_no = 71 + 50   # Night1 后段
        collected = 0
        for _ in range(8):
            r, t = brain.decide(sim.payload())
            if any(c.get("action") == "collect" for c in r["roleCommandMap"].values()):
                collected += 1
            sim.apply(r)
            sim.advance()
        self.assertGreater(collected, 0, "机器人清空后夜间应继续采矿，而非无谓回防")


class TestPioneerStickyReturn(unittest.TestCase):
    def test_returning_sticky(self):
        """IKHYSK：一旦开始归位，不因 travel 估算抖动切回任务（白天不震荡）。"""
        sim = make_sim(gold=0)
        sim.round_no = 65  # Day1 白天后段（距天黑 5 回合）
        turn = Turn.load(sim.payload())
        pioneer = next(u for u in turn.ours if u.kind == "pioneer")
        fsm = PioneerFSM()
        fsm.state = STATE_TASK_TRAVEL
        fsm.task_point = Pos(20, 20)
        ctx = type("C", (), {})()
        ctx.reserved = set()
        ctx.gunner_upgrade = None
        ctx.trace = {}
        ctx.note_pioneer = lambda *a, **k: None
        cp = Pos(9, 23)
        cmd = fsm._day_cmd(turn, pioneer, cp, ctx)
        self.assertTrue(fsm.returning, "近天黑应进入归位粘性")
        self.assertEqual(fsm.state, "RETURN_HOME")


class TestWallVoucherCap(unittest.TestCase):
    def test_wall_voucher_capped(self):
        """IKHYTB：墙券一次最多买 6 张（正面+转角），不得买 13 张饿死武器。"""
        sim = make_sim(gold=400)
        for i in range(14):
            sim.roles.append(sim._role(80000 + i, 7 + i % 7, 20 + i // 7, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        ctx = type("C", (), {})()
        ctx.weapon_need_l2 = False
        qty = fsm._voucher_qty(turn, unit, ctx, "WallUpgradeVoucher1", 20, "wall")
        self.assertLessEqual(qty, WALL_VOUCHER_BATCH)

    def test_weapon_reserve_limits_wall_buy(self):
        """武器未 L2 时，墙券购买要为武器券预留金币（金 200 → 墙券最多 (200-100)/20=5）。"""
        sim = make_sim(gold=200)
        for i in range(14):
            sim.roles.append(sim._role(81000 + i, 7 + i % 7, 20 + i // 7, "wall", 1000, level=1))
        turn = Turn.load(sim.payload())
        unit = next(u for u in turn.ours if u.unit_id == W1)
        fsm = WorkerFSM(W1)
        fsm.role = ROLE_REPAIRER
        ctx = type("C", (), {})()
        ctx.weapon_need_l2 = True
        qty = fsm._voucher_qty(turn, unit, ctx, "WallUpgradeVoucher1", 20, "wall")
        self.assertLessEqual(qty, 5)


class TestNewsWindow(unittest.TestCase):
    def test_iron_collapse_window(self):
        """IKHYTB：铁矿塌方"明天+2天"→ 预测 iron 生效日=day+1、持续 2 天。"""
        ne = NewsEconomy()
        news = ("矿业管理局紧急通报：北部铁矿区昨夜发生严重矿井塌方事故，主巷道结构受损。"
                "为保障矿工安全，矿区将于明日全面停工。类似规模的塌方事故，修复工程通常需要2天左右。")
        ne.update(news, 2)
        self.assertTrue(ne.last_parsed, "应确定性解析出铁矿停工")
        p = ne.predictions.get("iron")
        self.assertIsNotNone(p)
        self.assertEqual(p["start_day"], 3)   # 明天 = day+1
        self.assertEqual(p["days"], 2)
        self.assertGreater(ne.boost("iron", 3), 0, "D3 应触发高价售卖加权")
        self.assertEqual(ne.boost("iron", 5), 0.0, "D5 应已过期")

    def test_time_words(self):
        ne = NewsEconomy()
        ne.update("铜矿发生故障，后天起停止开采，持续3天", 1)
        p = ne.predictions.get("copper")
        self.assertIsNotNone(p)
        self.assertEqual(p["start_day"], 3)   # 后天 = day+2
        self.assertEqual(p["days"], 3)

    def test_unrecognized_triggers_llm_flag(self):
        ne = NewsEconomy()
        ne.update("边境传来难以名状的低鸣", 1)
        self.assertTrue(ne.last_new)
        self.assertFalse(ne.last_parsed)   # 未识别 → 上层走 LLM 兜底


class TestSopByType(unittest.TestCase):
    def test_sop_keyed_by_type_and_api_facts_reused(self):
        """IKHYTW §12.6：SOP 按任务类型固化（非文件名），跨城市复用；成功命令沉淀 API 经验。"""
        from agent.planners.task import TaskPlanner, TaskSession
        p = TaskPlanner()
        s = TaskSession()
        s.task_type = "api"
        s.task_text = "请阅读task_1_beijing.md"
        s.answer = '{"city":"北京","total_count":15}'
        s.cmd_history = [
            ("curl -H 'Authorization: Bearer k' "
             "'http://localhost:8899/api/v1/heritage/search?location=北京'",
             '[exitCode:0]\n{"code":200,"data":{"records":[]}}'),
        ]
        p._maybe_extract_sop(s)
        self.assertIn("api", p.sop, "SOP 应按类型固化")
        self.assertNotIn("beijing", p.sop, "不应按任务文件名固化")
        self.assertEqual(p.api_facts.get("auth"), "Authorization: Bearer <key>")
        self.assertEqual(p.api_facts.get("param"), "location")
        # 下一个 API 任务（南京）的 prompt 应带上经验（避免重新横跳）
        s2 = TaskSession()
        s2.task_type = "api"
        s2.task_text = "请阅读task_1_nanjing.md"
        prompt = p._build_prompt(s2)
        self.assertIn("跨任务 API 经验", prompt)
        self.assertIn("location", prompt)
        self.assertIn("Bearer", prompt)


if __name__ == "__main__":
    unittest.main()
