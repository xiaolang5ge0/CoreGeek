"""32进16 新特性测试：imp/destroy/catch/驾驶移动/召唤令/可控机器人/lastCmdResult 新格式。

规则依据：32_docs/任务书.md v2.0、32_docs/接口文档.md v2.0、32_docs/需求变更.md。
"""
import copy
import json
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.fsm_imp import ImpFSM
from agent.protocol import (
    Pos,
    Turn,
    catch_command,
    destroy_command,
    move_command,
    parse_cmd_result,
)
from agent.rules import LegalityGuard


def base_payload(round_no: int = 40) -> dict:
    """白天回合（(round_no-1)%130 < 70）。challenger 基地 (10,24)（上半区）。"""
    return {
        "roundNo": round_no,
        "mapInfo": {
            "width": 41,
            "height": 32,
            "zones": [
                {"neutralType": "copper", "pos": {"x": 25, "y": 8}, "remain": 6},   # 敌方半区
                {"neutralType": "stone", "pos": {"x": 12, "y": 22}, "remain": 4},   # 我方半区
                {"neutralType": "vendor", "pos": {"x": 20, "y": 16}, "remain": None},
                {"neutralType": "weaponShop", "pos": {"x": 25, "y": 20}, "remain": None},
                {"neutralType": "challengerVehicle", "pos": {"x": 14, "y": 20}, "remain": None},
                {"neutralType": "defenderVehicle", "pos": {"x": 26, "y": 12}, "remain": None},
            ],
        },
        "teamOur": {
            "type": "challenger",
            "teamId": "t1",
            "teamName": "T1",
            "goldNum": 100,
            "totalScore": 0,
            "playerTasks": [],
            "summonRobotList": [
                {"id": 30001, "pos": {"x": 8, "y": 20}, "roleType": "smallRobot",
                 "health": 40, "abnormalState": "", "targetTeam": ""},
            ],
            "roles": [
                {"id": 10013, "pos": {"x": 10, "y": 24}, "roleType": "station",
                 "health": 1500, "attackPower": 0, "attackRange": 0, "level": 1,
                 "isDriving": False},
                {"id": 10010, "pos": {"x": 11, "y": 22}, "roleType": "worker",
                 "health": 500, "attackPower": 0, "attackRange": 0,
                 "backPackCapability": 100, "backpack": [], "isDriving": False},
                {"id": 10011, "pos": {"x": 9, "y": 24}, "roleType": "pioneer",
                 "health": 500, "attackPower": 0, "attackRange": 0,
                 "backPackCapability": 40, "backpack": [], "isDriving": False},
                {"id": 10014, "pos": {"x": 24, "y": 8}, "roleType": "imp",
                 "health": 500, "attackPower": 0, "attackRange": 0,
                 "backPackCapability": 0, "backpack": [], "isDriving": False},
                {"id": 10015, "pos": {"x": 13, "y": 20}, "roleType": "worker",
                 "health": 500, "attackPower": 0, "attackRange": 0,
                 "backPackCapability": 100, "backpack": [], "isDriving": True},
            ],
        },
        "teamEnemy": {"roles": [
            {"id": 20013, "pos": {"x": 30, "y": 10}, "roleType": "station",
             "health": 1500, "attackPower": 0, "attackRange": 0, "level": 1},
        ]},
        "robot": {"roles": []},
        "phaseTask": "",
        "lastRoundRoleActionResults": {},
        "lastSummonTreasureResult": 0,
        "llmResp": "",
        "worldNews": {"officialNews": "", "folkLegends": ""},
        "lastCmdResult": "",
        "vendorShopList": [
            {"name": "stone", "price": 2},
            {"name": "iron", "price": 5},
            {"name": "copper", "price": 8},
        ],
        "weaponShopList": [{"name": "SmallRobotSummonOrder", "price": 15}],
        "errors": [],
    }


def turn_at(round_no: int = 40, **overrides) -> Turn:
    payload = base_payload(round_no)
    payload.update(copy.deepcopy(overrides))
    return Turn.load(payload)


class TestTurnParsing(unittest.TestCase):
    def test_mine_remain_parsed(self):
        turn = turn_at()
        self.assertEqual(turn.mine_remain.get(Pos(25, 8)), 6)
        self.assertEqual(turn.mine_remain.get(Pos(12, 22)), 4)
        self.assertNotIn(Pos(20, 16), turn.mine_remain)  # 非矿区无 remain

    def test_summon_robots_parsed(self):
        turn = turn_at()
        self.assertEqual(len(turn.summon_robots), 1)
        self.assertEqual(turn.summon_robots[0].robot_id, 30001)

    def test_is_driving_parsed(self):
        turn = turn_at()
        driving = next(u for u in turn.ours if u.unit_id == 10015)
        self.assertTrue(driving.is_driving)
        worker = next(u for u in turn.ours if u.unit_id == 10010)
        self.assertFalse(worker.is_driving)

    def test_half_of(self):
        turn = turn_at()
        self.assertTrue(turn.own_half(Pos(12, 22)))   # 我方石矿（上半区）
        self.assertFalse(turn.own_half(Pos(25, 8)))   # 铜矿（下半区=敌方）


class TestParseCmdResult(unittest.TestCase):
    def test_ok_format(self):
        r = parse_cmd_result("[exitCode:0]\n[durationMs:120]\nhello\nworld")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["duration_ms"], 120)
        self.assertEqual(r["output"], "hello\nworld")

    def test_timeout_format(self):
        r = parse_cmd_result("[TIMEOUT]\n[durationMs:15000]\npartial output")
        self.assertEqual(r["status"], "timeout")
        self.assertIn("[TIMEOUT]", r["output"])
        self.assertIn("partial output", r["output"])

    def test_judger_error_format(self):
        r = parse_cmd_result("[JUDGER_ERROR]\n[durationMs:5]\nsandbox broken")
        self.assertEqual(r["status"], "judger_error")
        self.assertIn("sandbox broken", r["output"])

    def test_truncated_flag(self):
        r = parse_cmd_result("[exitCode:0]\n[durationMs:1]\ndata...\n[TRUNCATED]")
        self.assertTrue(r["truncated"])

    def test_legacy_passthrough(self):
        r = parse_cmd_result("plain old output")
        self.assertEqual(r["status"], "legacy")
        self.assertEqual(r["output"], "plain old output")

    def test_empty(self):
        r = parse_cmd_result("")
        self.assertEqual(r["status"], "legacy")
        self.assertEqual(r["output"], "")


class TestDestroyCatchRules(unittest.TestCase):
    def test_destroy_by_imp_legal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10014, destroy_command(Pos(25, 8)))  # imp(24,8) 邻接铜矿
        self.assertTrue(verdict.ok, verdict.reason)

    def test_destroy_by_worker_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, destroy_command(Pos(12, 22)))
        self.assertEqual(verdict.reason, "imp_only")

    def test_destroy_not_mine_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10014, destroy_command(Pos(24, 9)))  # 邻接空地，不是矿
        self.assertEqual(verdict.reason, "no_mine_there")

    def test_destroy_missing_target_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10014, {"action": "destroy"})
        self.assertEqual(verdict.reason, "bad_targetPos")

    def test_catch_visible_enemy_imp_legal(self):
        payload = base_payload()
        payload["teamEnemy"]["roles"].append(
            {"id": 20014, "pos": {"x": 12, "y": 22}, "roleType": "imp",
             "health": 500, "attackPower": 0, "attackRange": 0}
        )
        turn = Turn.load(payload)
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, catch_command(Pos(12, 22)))  # worker(11,22) 邻接
        self.assertTrue(verdict.ok, verdict.reason)

    def test_catch_no_imp_there_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, catch_command(Pos(12, 22)))
        self.assertEqual(verdict.reason, "no_enemy_imp_there")


class TestDrivingMove(unittest.TestCase):
    def test_move_two_cells_when_driving(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10015, move_command(Pos(13, 18)))  # (13,20)→(13,18) 2格
        self.assertTrue(verdict.ok, verdict.reason)

    def test_move_two_cells_without_vehicle_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, move_command(Pos(11, 20)))
        self.assertEqual(verdict.reason, "move_not_adjacent")

    def test_own_vehicle_cell_not_blocked(self):
        turn = turn_at()
        worker = next(u for u in turn.ours if u.unit_id == 10010)
        self.assertNotIn(Pos(14, 20), turn.blocked(worker))   # 己方车格可驶入
        self.assertIn(Pos(26, 12), turn.blocked(worker))      # 敌方车格阻挡

    def test_build_on_vehicle_cell_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        # 驾驶中的工人 10015 在 (13,20)，紧邻己方小车格 (14,20)
        verdict = guard.check(10015, {
            "action": "build", "name": "wall", "targetPos": [{"x": 14, "y": 20}]
        })
        self.assertEqual(verdict.reason, "target_on_vehicle_cell")


class TestSummonOrderRules(unittest.TestCase):
    def _turn_with_summon_order(self):
        payload = base_payload()
        for role in payload["teamOur"]["roles"]:
            if role["id"] == 10010:
                role["backpack"] = ["SmallRobotSummonOrder"]
        return Turn.load(payload)

    def _use(self, pos=None):
        cmd = {"action": "use", "name": "SmallRobotSummonOrder"}
        if pos is not None:
            cmd["targetPos"] = [pos.dump()]
        return cmd

    def test_use_without_targetpos_illegal(self):
        guard = LegalityGuard(self._turn_with_summon_order())
        verdict = guard.check(10010, self._use())
        self.assertEqual(verdict.reason, "summon_need_targetPos")

    def test_use_in_build_zone_illegal(self):
        guard = LegalityGuard(self._turn_with_summon_order())
        verdict = guard.check(10010, self._use(Pos(9, 22)))  # 距基地 footprint 1（蓝区）
        self.assertEqual(verdict.reason, "summon_in_build_zone")

    def test_use_on_npc_illegal(self):
        guard = LegalityGuard(self._turn_with_summon_order())
        verdict = guard.check(10010, self._use(Pos(20, 16)))  # 小贩处
        self.assertEqual(verdict.reason, "summon_on_npc_or_blocked")

    def test_use_valid_position_legal(self):
        guard = LegalityGuard(self._turn_with_summon_order())
        verdict = guard.check(10010, self._use(Pos(16, 16)))
        self.assertTrue(verdict.ok, verdict.reason)


class TestSummonRobotCommands(unittest.TestCase):
    def test_robot_move_legal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(30001, move_command(Pos(9, 21)))
        self.assertTrue(verdict.ok, verdict.reason)

    def test_robot_unknown_id_illegal(self):
        turn = turn_at()
        guard = LegalityGuard(turn)
        verdict = guard.check(30099, move_command(Pos(9, 21)))
        self.assertEqual(verdict.reason, "robot_not_found")

    def test_robot_attack_night_legal(self):
        turn = turn_at(100)  # 夜晚
        guard = LegalityGuard(turn)
        verdict = guard.check(30001, {"action": "attack", "targetPos": [{"x": 9, "y": 22}]})
        self.assertTrue(verdict.ok, verdict.reason)

    def test_robot_attack_day_illegal(self):
        turn = turn_at(40)  # 白天
        guard = LegalityGuard(turn)
        verdict = guard.check(30001, {"action": "attack", "targetPos": [{"x": 9, "y": 22}]})
        self.assertEqual(verdict.reason, "night_only")

    def test_robot_attack_out_of_range(self):
        turn = turn_at(100)
        guard = LegalityGuard(turn)
        verdict = guard.check(30001, {"action": "attack", "targetPos": [{"x": 20, "y": 30}]})
        self.assertEqual(verdict.reason, "target_out_of_range")

    def test_robot_build_illegal(self):
        turn = turn_at(40)
        guard = LegalityGuard(turn)
        verdict = guard.check(30001, {
            "action": "build", "name": "wall", "targetPos": [{"x": 8, "y": 21}]
        })
        self.assertTrue(verdict.reason.startswith("robot_action_unknown"))


class TestImpFSM(unittest.TestCase):
    def _ctx(self):
        class Ctx:
            trace = {}
            reserved = frozenset()
            robot_cells = ()
            safe_anchor = None
            home_anchor = None
            note = staticmethod(lambda *a, **k: None)
        return Ctx()

    def test_imp_destroys_adjacent_enemy_half_mine(self):
        turn = turn_at()
        imp = turn.imp()
        fsm = ImpFSM()
        cmd = fsm.decide(turn, imp, self._ctx())
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "destroy")
        self.assertEqual(cmd["targetPos"], [{"x": 25, "y": 8}])

    def test_imp_catches_adjacent_foe_imp_first(self):
        payload = base_payload()
        payload["teamEnemy"]["roles"].append(
            {"id": 20014, "pos": {"x": 25, "y": 8}, "roleType": "imp",
             "health": 500, "attackPower": 0, "attackRange": 0}
        )
        turn = Turn.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), self._ctx())
        self.assertEqual(cmd["action"], "catch")

    def test_imp_flees_from_enemy(self):
        payload = base_payload()
        # 敌方角色贴脸（距离2 → 撤退）
        payload["teamEnemy"]["roles"].append(
            {"id": 20011, "pos": {"x": 25, "y": 7}, "roleType": "pioneer",
             "health": 500, "attackPower": 0, "attackRange": 0}
        )
        turn = Turn.load(payload)
        fsm = ImpFSM()
        cmd = fsm.decide(turn, turn.imp(), self._ctx())
        self.assertEqual(cmd["action"], "move")


class TestBrainCatchDefense(unittest.TestCase):
    def test_worker_catches_enemy_imp_in_own_half(self):
        payload = base_payload()
        # 敌方 imp 在我方半区石矿旁站桩，且与 worker 10010 邻接
        payload["teamEnemy"]["roles"].append(
            {"id": 20014, "pos": {"x": 12, "y": 22}, "roleType": "imp",
             "health": 500, "attackPower": 0, "attackRange": 0}
        )
        brain = Brain()
        response, trace = brain.decide(payload)
        self.assertIsNotNone(trace)
        cmd = response["roleCommandMap"].get("10010")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd["action"], "catch")
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 22}])


if __name__ == "__main__":
    unittest.main()
