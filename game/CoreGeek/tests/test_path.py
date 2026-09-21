"""L4 路径规划单元测试。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.path import approach_cells, find_path, next_step, step_toward
from agent.protocol import Pos, Turn, Unit, distance


def make_turn(mines=None):
    return Turn.load(SimWorld(mines=mines).payload())


class TestPath(unittest.TestCase):
    def test_straight(self):
        turn = make_turn()
        worker = turn.find(10010)  # (9,23)
        step = next_step(turn, worker, Pos(15, 23))
        # 任意最优步：相邻且缩短切比雪夫距离
        self.assertIsNotNone(step)
        self.assertEqual(distance(step, worker.pos), 1)
        self.assertLess(distance(step, Pos(15, 23)), distance(worker.pos, Pos(15, 23)))

    def test_around_base(self):
        turn = make_turn()
        worker = turn.find(10012)  # (9,22)
        path = find_path(turn, worker, Pos(12, 24))
        self.assertIsNotNone(path)
        base = {Pos(10, 24), Pos(11, 24), Pos(10, 23), Pos(11, 23)}
        self.assertTrue(base.isdisjoint(path))

    def test_reserved_blocks(self):
        turn = make_turn()
        worker = turn.find(10010)
        step = next_step(turn, worker, Pos(15, 23), reserved=frozenset({Pos(10, 23)}))
        self.assertNotEqual(step, Pos(10, 23))

    def test_approach_mine(self):
        turn = make_turn(mines={(6, 22): "stone"})
        worker = turn.find(10010)  # (9,23)
        cells = approach_cells(turn, worker, Pos(6, 22))
        self.assertTrue(cells)
        for cell in cells:
            self.assertLessEqual(distance(cell, Pos(6, 22)), 1)
            self.assertNotEqual(cell, Pos(6, 22))

    def test_step_toward_mine_and_arrive(self):
        turn = make_turn(mines={(6, 22): "stone"})
        worker = turn.find(10010)  # (9,23) 距矿 3
        step = step_toward(turn, worker, Pos(6, 22))
        self.assertIsNotNone(step)
        # 已在邻接格 → None
        worker_moved = Unit(
            worker.unit_id, Pos(7, 22), worker.kind, worker.health, worker.level,
            worker.cooldown, worker.attack_range, worker.attack_power,
            worker.capacity, worker.backpack,
        )
        self.assertIsNone(step_toward(turn, worker_moved, Pos(6, 22)))


if __name__ == "__main__":
    unittest.main()
