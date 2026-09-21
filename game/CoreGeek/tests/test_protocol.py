"""L1 协议层单元测试。"""
import json
import unittest

import _bootstrap  # noqa: F401  (sys.path 引导)

from agent.protocol import (
    DAY_ROUNDS,
    ROUNDS_PER_DAY,
    Pos,
    Turn,
    attack_command,
    build_command,
    build_response,
    collect_command,
    distance,
    move_command,
    sell_command,
    station_footprint,
)


def load_turn(round_no: int | None = None) -> Turn:
    payload = json.loads(_bootstrap.FIXTURE.read_text(encoding="utf-8"))
    if round_no is not None:
        payload["roundNo"] = round_no
    return Turn.load(payload)


class TestGeometry(unittest.TestCase):
    def test_chebyshev(self):
        self.assertEqual(distance(Pos(0, 0), Pos(3, -2)), 3)
        self.assertEqual(distance(Pos(5, 5), Pos(5, 5)), 0)

    def test_station_footprint(self):
        fp = station_footprint(Pos(10, 24))
        self.assertEqual(
            set(fp),
            {Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23)},
        )

    def test_neighbours(self):
        self.assertEqual(len(Pos(3, 3).neighbours()), 8)


class TestTime(unittest.TestCase):
    def check(self, round_no, day, is_day):
        turn = load_turn(round_no)
        self.assertEqual(turn.day_index, day, f"round {round_no}")
        self.assertEqual(turn.is_day, is_day, f"round {round_no}")

    def test_boundaries(self):
        self.check(1, 1, True)
        self.check(70, 1, True)
        self.check(71, 1, False)
        self.check(130, 1, False)
        self.check(131, 2, True)
        self.check(1300, 10, False)

    def test_rounds_until(self):
        turn = load_turn(1)
        self.assertEqual(turn.rounds_until_night, DAY_ROUNDS)
        turn = load_turn(71)
        self.assertEqual(turn.rounds_until_dawn, ROUNDS_PER_DAY - 70)


class TestTurnLoad(unittest.TestCase):
    def test_fixture_fields(self):
        turn = load_turn()  # round 85 → 第1天黑夜
        self.assertEqual(turn.round_no, 85)
        self.assertTrue(turn.is_night)
        self.assertEqual(turn.day_index, 1)
        self.assertEqual(turn.gold, 20)
        self.assertEqual((turn.width, turn.height), (41, 32))
        self.assertEqual(turn.station().pos, Pos(10, 24))
        self.assertEqual(len(turn.weapons()), 3)
        self.assertEqual(len(turn.workers()), 2)
        self.assertIsNotNone(turn.pioneer())
        self.assertEqual(len(turn.walls()), 2)
        self.assertEqual(len(turn.robots), 4)
        boss = [r for r in turn.robots if r.kind == "bossRobot"][0]
        self.assertTrue(boss.dizzy)
        self.assertEqual(turn.vendor_prices, {"stone": 1, "iron": 3, "copper": 5})
        self.assertEqual(turn.shop_prices["WeaponUpgradeVoucher1"], 100)
        self.assertEqual(turn.last_action_results[10010], False)
        self.assertEqual(turn.last_action_results[10011], True)
        self.assertEqual(len(turn.mines("stone")), 2)
        self.assertEqual(len(turn.mines()), 6)
        self.assertEqual(turn.vendor_positions(), (Pos(20, 16),))
        self.assertEqual(turn.errors[0][0], 2)

    def test_runtime_range_override(self):
        """request 示例 L1 加特林 attackRange=4（与任务书静态值 3 冲突）→ 运行时优先。"""
        turn = load_turn()
        gatling = next(w for w in turn.weapons() if w.kind == "gatling")
        self.assertEqual(gatling.range_of_attack(), 4)
        rocket = next(w for w in turn.weapons() if w.kind == "rocket")
        self.assertGreater(rocket.range_of_attack(), 10**8)


class TestCommands(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(
            move_command(Pos(1, 2)),
            {"action": "move", "targetPos": [{"x": 1, "y": 2}]},
        )
        atk = attack_command(10010, [Pos(3, 4)])
        self.assertEqual(atk["controllerId"], "10010")
        self.assertEqual(atk["targetPos"], [{"x": 3, "y": 4}])
        self.assertEqual(build_command(Pos(1, 1), "wall")["name"], "wall")
        self.assertEqual(sell_command("stone", 5)["num"], 5)
        self.assertEqual(collect_command(Pos(4, 24))["action"], "collect")

    def test_response(self):
        resp = build_response({10010: move_command(Pos(1, 1))})
        self.assertIn("10010", resp["roleCommandMap"])
        self.assertEqual(resp["prompt"], "")
        self.assertEqual(resp["executeCmd"], "")


if __name__ == "__main__":
    unittest.main()
