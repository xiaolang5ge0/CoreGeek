"""捣乱鬼全天候破坏 + 反抓追击派单（用户 2026-10-09 裁决）回归测试。

裁决内容：
1. imp 不分昼夜/天数常驻敌方半区毁矿，纪律只有"不死"（撤退/包夹/机器人避让/拉黑换目标）。
2. 防御只采纳三点：邻接即抓（已有）/ 站桩中最近工人 2-4 步派单追击 / 永不派开拓者追击。
"""
import copy
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.fsm_imp import (
    COMBO_WINDOW,
    DESTROY_ROUNDS,
    FLEE_DIST,
    MINE_BLACKLIST_ROUNDS,
    ImpFSM,
)
from agent.protocol import Pos, Turn, distance
from test_v2_rules import base_payload, turn_at


def _imp_ctx():
    class Ctx:
        trace = {}
        reserved = frozenset()
        robot_cells = ()
        safe_anchor = None
        home_anchor = None
        note = staticmethod(lambda *a, **k: None)
    return Ctx()


def _foe_imp(x: int, y: int) -> dict:
    return {"id": 20014, "pos": {"x": x, "y": y}, "roleType": "imp",
            "health": 500, "attackPower": 0, "attackRange": 0}


def _foe_worker(x: int, y: int, id_: int = 20010) -> dict:
    return {"id": id_, "pos": {"x": x, "y": y}, "roleType": "worker",
            "health": 500, "attackPower": 0, "attackRange": 0}


class TestImpAllWeather(unittest.TestCase):
    """全天候破坏：夜间不再撤退，站桩/避让/拉黑/连击。"""

    def test_imp_destroys_at_night_not_retreat(self):
        # round 75 → (75-1)%130=74 ≥70 黑夜；imp (24,8) 邻接敌方半区铜矿 (25,8)
        # IKKHUU-Q1-D：敌基地挪到 (40,10)（禁入圈外）——圈内夜间撤离是新预期行为
        turn = turn_at(75)
        for r in turn.enemy:
            if r.kind == "station":
                r.pos.load({"x": 40, "y": 10}) if hasattr(r.pos, "load") else None
        # Pos 是 frozen dataclass → 直接改 payload 重建
        from test_v2_rules import base_payload
        from agent.protocol import Turn as T
        payload = base_payload(75)
        for r in payload["teamEnemy"]["roles"]:
            if r.get("roleType") == "station":
                r["pos"] = {"x": 40, "y": 10}
        turn = T.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "destroy")
        self.assertEqual(cmd["targetPos"], [{"x": 25, "y": 8}])

    def test_imp_evades_robot_at_night(self):
        payload = base_payload(75)
        payload["robot"]["roles"] = [
            {"id": 90001, "pos": {"x": 24, "y": 9}, "roleType": "smallRobot",
             "health": 40, "abnormalState": "", "targetTeam": "t1"},
        ]
        turn = Turn.load(payload)
        imp = turn.imp()
        fsm = ImpFSM()
        cmd = fsm.decide(turn, imp, _imp_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertGreater(distance(step, Pos(24, 9)), distance(imp.pos, Pos(24, 9)))

    def test_robot_evade_breaks_stance_and_blacklists(self):
        payload = base_payload(75)
        payload["robot"]["roles"] = [
            {"id": 90001, "pos": {"x": 24, "y": 9}, "roleType": "smallRobot",
             "health": 40, "abnormalState": "", "targetTeam": "t1"},
        ]
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.destroy_target = Pos(25, 8)
        fsm.destroy_progress = 2
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "move")
        # 站桩被打破 → 拉黑 + 进度清零
        self.assertIn(Pos(25, 8), fsm.mine_blacklist)
        self.assertIsNone(fsm.destroy_target)
        self.assertEqual(fsm.destroy_progress, 0)

    def test_flee_keeps_target_without_blacklist(self):
        # IKKE6Q-Q1-B：flee 打断站桩 → 进度清零但**保留目标、不拉黑**（威胁暂态）
        turn = turn_at(40)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy")
        self.assertEqual(fsm.destroy_progress, 1)
        # 敌方 worker 贴脸 → 贴边游走（不拉黑）
        payload = base_payload(40)
        payload["teamEnemy"]["roles"].append(_foe_worker(24, 7))
        turn = Turn.load(payload)
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "move")
        self.assertNotIn(Pos(25, 8), fsm.mine_blacklist, "flee 打断不拉黑")
        self.assertEqual(fsm.destroy_target, Pos(25, 8), "目标保留，敌走后回桩")
        # 敌人走了 → 回到原矿继续站桩
        turn = turn_at(41)
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy")
        self.assertEqual(cmd["targetPos"], [{"x": 25, "y": 8}])

    def test_pincer_removed_no_flee_at_distance_4(self):
        # IKKE6Q-Q1-A：包夹判定删除——两个敌角色 4 格外（≤旧 PINCER_DIST 5）不触发 flee
        payload = base_payload(40)
        payload["teamEnemy"]["roles"].append(_foe_worker(20, 8, 20010))
        payload["teamEnemy"]["roles"].append(_foe_worker(28, 8, 20011))
        turn = Turn.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(cmd["action"], "destroy", "4 格外的敌人不构成威胁，继续站桩")

    def test_destroy_illegal_blacklists_and_stops(self):
        payload = base_payload(40)
        payload["lastRoundRoleActionResults"] = {"10014": False}
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.destroy_target = Pos(25, 8)
        fsm.destroy_progress = 3
        cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertIsNone(cmd)
        self.assertIn(Pos(25, 8), fsm.mine_blacklist)
        self.assertEqual(fsm.destroy_progress, 0)

    def test_four_round_destroy_records_combo(self):
        turn = turn_at(40)
        fsm = ImpFSM()
        for _ in range(DESTROY_ROUNDS):
            cmd = fsm.decide(turn, turn.imp(), _imp_ctx())
            self.assertEqual(cmd["action"], "destroy")
        self.assertEqual(fsm.last_destroyed, ("copper", 40))
        self.assertIsNone(fsm.destroy_target)
        self.assertEqual(fsm.destroy_progress, 0)

    def test_combo_bonus_prefers_same_kind(self):
        # 铜矿 (25,8) remain6×8=48 > 铁矿 (27,7) remain9×5=45 → 默认选铜
        payload = base_payload(40)
        payload["mapInfo"]["zones"].append(
            {"neutralType": "iron", "pos": {"x": 27, "y": 7}, "remain": 9}
        )
        turn = Turn.load(payload)
        fsm = ImpFSM()
        fsm.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm.destroy_target, Pos(25, 8))
        # 上次毁了 iron（窗口内）→ iron 45×1.5=67.5 > 48 → 连击换 iron
        fsm2 = ImpFSM()
        fsm2.last_destroyed = ("iron", 40 - 1)
        fsm2.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm2.destroy_target, Pos(27, 7))
        # 连击窗口外不再加成
        fsm3 = ImpFSM()
        fsm3.last_destroyed = ("iron", 40 - COMBO_WINDOW - 1)
        fsm3.decide(turn, turn.imp(), _imp_ctx())
        self.assertEqual(fsm3.destroy_target, Pos(25, 8))


class TestBrainHuntDispatch(unittest.TestCase):
    """反抓追击派单：站桩敌 imp + 工人 2-4 步 → 派单；不派开拓者；机器人贴身不派。"""

    def test_worker_dispatched_toward_stancing_foe(self):
        payload = base_payload(40)
        # 敌 imp 在我方半区、贴着我方石矿 (12,22)；worker 10010 距其 2 步
        payload["teamEnemy"]["roles"].append(_foe_imp(13, 22))
        brain = Brain()
        response, trace = brain.decide(payload)
        hunts = trace.get("catch_hunt", [])
        self.assertTrue(hunts, "应派工人追击站桩敌 imp")
        self.assertEqual(hunts[0]["role"], 10010)
        cmd = response["roleCommandMap"].get("10010")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        foe = Pos(13, 22)
        worker = Pos(11, 22)
        self.assertLess(distance(step, foe), distance(worker, foe))

    def test_pioneer_never_dispatched_for_hunt(self):
        payload = base_payload(40)
        # 敌 imp 贴石矿、距 pioneer 2 步；两个工人都移远
        payload["teamEnemy"]["roles"].append(_foe_imp(11, 23))
        for rid, x, y in ((10010, 5, 5), (10015, 5, 6)):
            for r in payload["teamOur"]["roles"]:
                if r["id"] == rid:
                    r["pos"] = {"x": x, "y": y}
        brain = Brain()
        response, trace = brain.decide(payload)
        self.assertEqual(trace.get("catch_hunt", []), [])
        cmd = response["roleCommandMap"].get("10011")  # pioneer
        self.assertTrue(cmd is None or cmd.get("action") != "catch")

    def test_no_hunt_when_robot_near_worker(self):
        payload = base_payload(75)  # 夜
        payload["teamEnemy"]["roles"].append(_foe_imp(13, 22))
        # 机器人 (12,21)：距 10010(11,22)=1、距 10015(13,20)=2，两个候选猎人都被守住
        payload["robot"]["roles"] = [
            {"id": 90001, "pos": {"x": 12, "y": 21}, "roleType": "smallRobot",
             "health": 40, "abnormalState": "", "targetTeam": "t1"},
        ]
        brain = Brain()
        _, trace = brain.decide(payload)
        self.assertEqual(trace.get("catch_hunt", []), [])

    def test_no_hunt_when_foe_not_adjacent_to_mine(self):
        payload = base_payload(40)
        # 敌 imp 在我方半区但不贴任何矿 → 非站桩信号 → 不追（用户未采纳路过追击）
        payload["teamEnemy"]["roles"].append(_foe_imp(10, 20))
        brain = Brain()
        _, trace = brain.decide(payload)
        self.assertEqual(trace.get("catch_hunt", []), [])


if __name__ == "__main__":
    unittest.main()
