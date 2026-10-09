"""IKKE6Q 裁决回归（2026-10-09）：
Q1-A 包夹判定删除（两敌 4 格外不 flee）；Q1-B flee 打断不拉黑、保留目标回桩；
Q1-C 敌基地消失 → imp 不再行动；Q1-D 贴边游走（flee 落点脱离危险且贴近目标矿）；
Q3-① 统计类任务口径纪律入 prompt；Q3-② 宝藏候选列表（ts=2 依次换候选）。
"""
import json
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.fsm_imp import ImpFSM
from agent.planners.task import TaskPlanner, TaskSession
from agent.planners.treasure import TreasurePlanner
from agent.protocol import Pos, Turn
from test_v2_rules import base_payload


def _imp_ctx():
    class Ctx:
        trace = {}
        reserved = frozenset()
        note = staticmethod(lambda *a, **k: None)
    return Ctx()


def _foe_worker(x, y, id_=20010):
    return {"id": id_, "pos": {"x": x, "y": y}, "roleType": "worker",
            "health": 500, "attackPower": 0, "attackRange": 0}


class TestQ1PincerRemoved(unittest.TestCase):
    def test_two_foes_at_4_no_flee(self):
        payload = base_payload(40)
        payload["teamEnemy"]["roles"].append(_foe_worker(20, 8))
        payload["teamEnemy"]["roles"].append(_foe_worker(28, 8))
        turn = Turn.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy", "包夹已删：4 格外双敌不触发 flee")


class TestQ1FleeKeepsTarget(unittest.TestCase):
    def test_flee_no_blacklist_and_resume(self):
        turn = turn_at = Turn.load(base_payload(40))
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy")
        payload = base_payload(40)
        payload["teamEnemy"]["roles"].append(_foe_worker(24, 7))
        turn = Turn.load(payload)
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "move")
        self.assertNotIn(Pos(25, 8), fsm.mine_blacklist)
        self.assertEqual(fsm.destroy_target, Pos(25, 8))
        turn = Turn.load(base_payload(41))
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy", "敌走后回原矿继续")


class TestQ1SentryFlee(unittest.TestCase):
    def test_flee_lands_safe_and_near_target(self):
        # 敌 worker 移动逼近 → imp 逐回合拉开（imp 位置跟随 flee 落点，模拟连续对局），
        # 落点始终贴近目标矿（放哨位）；敌静止后回桩
        fsm = ImpFSM()
        imp_pos = (24, 8)
        foe_pos = (24, 7)
        dists = []
        for i, rn in enumerate((40, 41, 42, 43, 44)):
            foe_pos = (24 + i, 7 + (i % 2))   # 敌每回合移动（追击中）→ 持续构成威胁
            payload = base_payload(rn)
            payload["teamEnemy"]["roles"].append(_foe_worker(*foe_pos))
            for u in payload["teamOur"]["roles"]:
                if u.get("roleType") == "imp":
                    u["pos"] = {"x": imp_pos[0], "y": imp_pos[1]}
            turn = Turn.load(payload)
            unit = next(u for u in turn.ours if u.kind == "imp")
            cmd = fsm.decide(turn, unit, _imp_ctx())
            self.assertIsNotNone(cmd)
            if i < 3:
                # 前几回合敌在动 → 贴边拉开
                self.assertEqual(cmd["action"], "move", f"r{rn} 应持续游走拉开")
                imp_pos = (cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                dists.append(distance(Pos(*imp_pos), Pos(*foe_pos)))
                self.assertLessEqual(distance(Pos(*imp_pos), Pos(*foe_pos)), 4,
                                     "贴边游走不逃远")
        self.assertTrue(dists and max(dists) >= 3, f"移动敌逼近应拉开距离：{dists}")

    def test_static_foe_no_threat(self):
        # IKKE6Q-Q1-A：静止采矿的敌工人邻接 → 首回合历史不足保守游走，
        # 确认敌 3 回合未动后 → 无视之，imp 回桩继续破坏
        fsm = ImpFSM()
        for i, rn in enumerate((40, 41, 42, 43)):
            payload = base_payload(rn)
            payload["teamEnemy"]["roles"].append(_foe_worker(24, 7))   # 同位置静止
            turn = Turn.load(payload)
            cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
            if i == 0:
                self.assertEqual(cmd["action"], "move",
                                 "首回合历史不足 → 保守游走")
            else:
                self.assertEqual(cmd["action"], "destroy",
                                 f"静止敌（{i} 回合同位置）不构成威胁 r{rn}")


from agent.protocol import distance  # noqa: E402


class TestQ1NoEnemyBase(unittest.TestCase):
    def test_no_enemy_station_imp_idle(self):
        payload = base_payload(40)
        # 移除敌方基地（被摧毁）
        payload["teamEnemy"]["roles"] = []
        turn = Turn.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertIsNone(cmd, "敌基地消失 → imp 不再行动")

    def test_pick_target_requires_enemy_station(self):
        payload = base_payload(40)
        payload["teamEnemy"]["roles"] = []
        turn = Turn.load(payload)
        fsm = ImpFSM()
        self.assertIsNone(fsm._pick_target(turn, turn.imp(), _imp_ctx()))


class TestQ3StatsDiscipline(unittest.TestCase):
    def test_prompt_has_stats_discipline_for_general(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "统计任务"
        s.task_type = "general"
        prompt = planner._build_prompt(s)
        self.assertIn("统计类作答纪律", prompt)
        self.assertIn("全量", prompt)

    def test_no_stats_discipline_for_engineering(self):
        planner = TaskPlanner()
        s = TaskSession()
        s.task_text = "工程任务"
        s.task_type = "engineering"
        prompt = planner._build_prompt(s)
        self.assertNotIn("统计类作答纪律", prompt)


class TestQ3TreasureCandidates(unittest.TestCase):
    def _planner_with_response(self):
        tp = TreasurePlanner()
        tp.observe("第4天传闻：旧账本扉页总号(18,18)，祭坛在总号之东两格、之北一格", 4)
        resp = json.dumps({
            "x": 20, "y": 17, "items": ["FrostPotion"], "day": 5, "ready": True,
            "candidates": [{"x": 20, "y": 19}, {"x": 18, "y": 18}],
            "reason": "test"})
        ok = tp.apply_llm(resp, current_day=5,
                          offerings=("FrostPotion", "IronWhistle"))
        return tp, ok

    def test_candidates_parsed(self):
        tp, ok = self._planner_with_response()
        self.assertTrue(ok)
        self.assertEqual(tp.plan.location, Pos(20, 17))
        self.assertEqual(tp.plan.candidates, (Pos(20, 19), Pos(18, 18)))

    def test_ts2_switches_to_next_candidate(self):
        tp, _ = self._planner_with_response()
        tp.on_summon_result(2)
        self.assertEqual(tp.plan.location, Pos(20, 19), "ts=2 → 切换到下一个候选")
        self.assertEqual(tp.plan.candidates, (Pos(18, 18),))
        self.assertFalse(tp.attempted)
        self.assertIn((20, 17), tp.failed_sites)

    def test_exhausted_candidates_fallback_to_repush(self):
        tp, _ = self._planner_with_response()
        tp.on_summon_result(2)   # → (20,19)
        tp.on_summon_result(2)   # → (18,18)
        tp.on_summon_result(2)   # 候选用尽 → 计划作废 + 重推（_last_infer_len=0）
        self.assertIsNone(tp.plan.location)
        self.assertTrue(tp.needs_inference(), "候选用尽 → 重推 LLM")
        self.assertEqual(len(tp.failed_sites), 3)

    def test_failed_sites_not_reselected(self):
        tp, _ = self._planner_with_response()
        tp.failed_sites.append((20, 19))
        tp.on_summon_result(2)   # 主选失败 → 候选 (20,19) 在 failed_sites → 跳过
        self.assertEqual(tp.plan.candidates, (Pos(18, 18),),
                         "失败过的候选不再纳入")


if __name__ == "__main__":
    unittest.main()
