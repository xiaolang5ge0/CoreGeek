"""接口文档回归：火箭/加特林 `targetPos` 个数必须 **等于当前武器等级**。

背景：夜末只剩 1~2 只机器人时，原实现只发 1~2 个目标 → 服务端判指令不生效 → 炮台整夜不开火、
残兵撑到天亮。修复：`fire` 补足到 `level` 个目标（邻近格优先）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.fire import JointFirePlanner
from agent.protocol import Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True


def _plan(weapon_level, robots):
    sim = SimWorld(station_pos=(10, 24), mines={})
    sim.roles.append(sim._role(50040, 9, 23, "rocket", 2000, level=weapon_level))
    for i, (x, y) in enumerate(robots):
        sim.spawn_robot(x, y, "middleRobot", hp=60, rid=30900 + i)
    turn = Turn.load(sim.payload())
    pioneer = next(u for u in turn.ours if u.kind == "pioneer")
    commands, trace = {}, {}
    JointFirePlanner().plan(turn, pioneer, commands, trace)
    return commands, trace


class TestFireTargetCount(unittest.TestCase):
    def test_pads_to_weapon_level(self):
        """L3 火箭 + 只有 1 只机器人 → 仍必须发 3 个目标。"""
        commands, trace = _plan(3, [(12, 23)])
        cmd = commands.get(50040)
        self.assertIsNotNone(cmd, "应发起攻击（补足目标数后才合法）")
        self.assertEqual(len(cmd["targetPos"]), 3, "火箭目标数必须 = 武器等级(3)")

    def test_l2_pads_to_two(self):
        commands, _ = _plan(2, [(12, 23)])
        cmd = commands.get(50040)
        self.assertIsNotNone(cmd)
        self.assertEqual(len(cmd["targetPos"]), 2)

    def test_l1_single_target(self):
        commands, _ = _plan(1, [(12, 23)])
        cmd = commands.get(50040)
        self.assertIsNotNone(cmd)
        self.assertEqual(len(cmd["targetPos"]), 1)

    def test_no_robot_no_attack(self):
        commands, trace = _plan(3, [])
        self.assertNotIn(50040, commands)


class TestAttackLegalityExactCount(unittest.TestCase):
    def test_fewer_targets_is_illegal(self):
        """规则层：目标数与武器等级不一致 → 非法（接口文档要求相同）。"""
        from agent.rules import LegalityGuard
        sim = SimWorld(station_pos=(10, 24), mines={})
        sim.roles.append(sim._role(50040, 9, 23, "rocket", 2000, level=3))
        sim.spawn_robot(12, 23, "middleRobot", hp=60)
        turn = Turn.load(sim.payload())
        guard = LegalityGuard(turn)
        weapon = next(u for u in turn.ours if u.unit_id == 50040)
        bad = {"action": "attack", "controllerId": "10011",
               "targetPos": [{"x": 12, "y": 23}]}
        self.assertFalse(guard.check(50040, bad).ok, "L3 火箭只给 1 个目标应判非法")


if __name__ == "__main__":
    unittest.main()