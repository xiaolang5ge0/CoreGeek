"""L2 LegalityGuard 单元测试。"""
import copy
import json
import unittest

import _bootstrap  # noqa: F401

from agent.protocol import Pos, Turn, attack_command, build_command, collect_command, move_command, sell_command
from agent.rules import LegalityGuard, cone_ok


def load_payload(round_no: int | None = None) -> dict:
    payload = json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
    if round_no is not None:
        payload["roundNo"] = round_no
    return payload


def turn_at(round_no: int) -> Turn:
    return Turn.load(load_payload(round_no))


class TestCone(unittest.TestCase):
    def test_within_90(self):
        origin = Pos(10, 10)
        self.assertTrue(cone_ok(origin, (Pos(15, 10), Pos(10, 15))))
        self.assertTrue(cone_ok(origin, (Pos(13, 11),)))

    def test_over_90(self):
        origin = Pos(10, 10)
        self.assertFalse(cone_ok(origin, (Pos(15, 10), Pos(5, 10))))
        self.assertFalse(cone_ok(origin, (Pos(15, 15), Pos(4, 12))))

    def test_zero_vector(self):
        self.assertFalse(cone_ok(Pos(10, 10), (Pos(10, 10),)))


class TestTimeRules(unittest.TestCase):
    def test_attack_day_illegal(self):
        turn = turn_at(40)  # 白天
        guard = LegalityGuard(turn)
        verdict = guard.check(10020, attack_command("10010", [Pos(9, 20)]))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "night_only")

    def test_build_night_illegal(self):
        turn = turn_at(85)  # 黑夜（fixture 原始回合）
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, build_command(Pos(6, 23), "wall"))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "day_only")


class TestActionRules(unittest.TestCase):
    def test_collect_legal(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        # 工人 10010 在 (5,23)，石矿在 (4,24)，相邻
        verdict = guard.check(10010, collect_command(Pos(4, 24)))
        self.assertTrue(verdict.ok, verdict.reason)

    def test_collect_pioneer_forbidden(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        verdict = guard.check(10011, collect_command(Pos(4, 24)))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "worker_only")

    def test_move_not_adjacent(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, move_command(Pos(8, 23)))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "move_not_adjacent")

    def test_sell_requires_vendor(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        # 工人 10010 在 (5,23) 背包有 stone，小贩在 (20,16)，不相邻
        verdict = guard.check(10010, sell_command("stone", 1))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "vendor_not_adjacent")

    def test_attack_controller_not_adjacent(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        # 工人 10010 在 (5,23)，距加特林 (9,24) 为 4
        verdict = guard.check(10020, attack_command(10010, [Pos(6, 21)]))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "controller_not_adjacent")

    def test_attack_legal_and_range(self):
        payload = load_payload(85)
        # 把工人 10010 挪到 (8,24)，与加特林(9,24)、火箭(9,25)均相邻
        for role in payload["teamOur"]["roles"]:
            if role["id"] == 10010:
                role["pos"] = {"x": 8, "y": 24}
        turn = Turn.load(payload)
        guard = LegalityGuard(turn)
        # 加特林 L1 射程 4（运行时覆盖），(6,21) 距离 3 → 合法
        self.assertTrue(guard.check(10020, attack_command(10010, [Pos(6, 21)])).ok)
        # (4,4) 距离 20 → 超程
        verdict = guard.check(10020, attack_command(10010, [Pos(4, 4)]))
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "target_out_of_range")
        # 火箭射程 INT_MAX → 合法
        self.assertTrue(guard.check(10040, attack_command(10010, [Pos(4, 4)])).ok)

    def test_controller_busy(self):
        payload = load_payload(85)
        for role in payload["teamOur"]["roles"]:
            if role["id"] == 10010:
                role["pos"] = {"x": 8, "y": 24}
        turn = Turn.load(payload)
        guard = LegalityGuard(turn)
        commands = {
            10020: attack_command(10010, [Pos(6, 21)]),
            10040: attack_command(10010, [Pos(4, 4)]),
        }
        trace = {}
        accepted = guard.filter_commands(commands, trace)
        self.assertEqual(len(accepted), 1)
        self.assertIn("controller_busy", trace["guard_dropped"].values())

    def test_build_wall_day(self):
        payload = load_payload(40)
        for role in payload["teamOur"]["roles"]:
            if role["id"] == 10010:
                role["pos"] = {"x": 8, "y": 22}  # 邻接黄区格 (8,23)
        turn = Turn.load(payload)
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, build_command(Pos(8, 23), "wall"))
        self.assertTrue(verdict.ok, verdict.reason)

    def test_unknown_action(self):
        turn = turn_at(85)
        guard = LegalityGuard(turn)
        self.assertFalse(guard.check(10010, {"action": "fly"}).ok)


class TestBuildZones(unittest.TestCase):
    """建造区规则（已确认）：武器=距基地1格（蓝区），墙=距基地2格（黄区）。"""

    def day_turn_with_worker_at(self, x, y):
        payload = load_payload(40)  # 白天
        for role in payload["teamOur"]["roles"]:
            if role["id"] == 10010:
                role["pos"] = {"x": x, "y": y}
        return Turn.load(payload)

    def test_wall_zone(self):
        # 基地 (10,24)：黄区格 (8,23) 合法；蓝区格 (9,23) 建墙非法
        turn = self.day_turn_with_worker_at(8, 22)
        guard = LegalityGuard(turn)
        ok = guard.check(10010, build_command(Pos(8, 23), "wall"))
        self.assertTrue(ok.ok, ok.reason)
        bad = guard.check(10010, build_command(Pos(9, 23), "wall"))
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, "wall_zone_not_dist2")

    def test_weapon_zone(self):
        turn = self.day_turn_with_worker_at(8, 22)
        guard = LegalityGuard(turn)
        bad = guard.check(10010, build_command(Pos(8, 23), "rocket"))
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, "weapon_zone_not_dist1")

    def test_remove_night_conservative(self):
        turn = turn_at(85)  # 黑夜
        guard = LegalityGuard(turn)
        verdict = guard.check(10010, {"action": "remove", "targetPos": [{"x": 5, "y": 20}]})
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "day_only")


if __name__ == "__main__":
    unittest.main()
