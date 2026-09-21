"""L3 BaseLayoutPlanner 单元测试。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.planners.layout import choose_front, compute_layout
from agent.protocol import Pos, Turn, distance, station_footprint


def make_turn(station=(10, 24), mines=None):
    return Turn.load(SimWorld(station_pos=station, mines=mines).payload())


class TestChooseFront(unittest.TestCase):
    def test_topleft_base_opens_west(self):
        """challenger 左上：机器人从右(东)来 → 开口朝西，墙在 右/上/下。"""
        turn = make_turn((10, 24))
        self.assertEqual(choose_front(turn), "W")

    def test_bottomright_base_opens_east(self):
        """defender 右下：机器人从左(西)来 → 开口朝东，墙在 左/上/下。"""
        turn = make_turn((30, 7))
        self.assertEqual(choose_front(turn), "E")

    def test_wall_side_faces_robots(self):
        """FRONT=W：墙环含东侧（来敌侧）列、不含西侧（开口侧）列。"""
        turn = make_turn((10, 24))
        layout = compute_layout(turn, "W")
        walls = set(layout.wall_cells)
        self.assertIn(Pos(13, 23), walls)      # 东侧列（距基地2格，x=xmax+2）
        self.assertNotIn(Pos(8, 23), walls)    # 西侧列开口
        # 炮台在西侧（背向来敌），与 CP 两两相邻
        for turret in layout.turret_cells:
            self.assertLessEqual(turret.x, 10)


class TestLayout(unittest.TestCase):
    def check_layout(self, station, front):
        turn = make_turn(station)
        layout = compute_layout(turn, front)
        self.assertIsNotNone(layout)
        base = set(station_footprint(turn.station().pos))
        # 3 炮台 + 控制点两两相邻（一站控三炮的几何前提）
        self.assertEqual(len(layout.turret_cells), 3)
        for turret in layout.turret_cells:
            self.assertLessEqual(distance(turret, layout.control_point), 1)
            self.assertNotIn(turret, base)
        # 墙：约 14 格（地图内无遮挡时），不与基地重叠
        self.assertGreaterEqual(len(layout.wall_cells), 10)
        for wall in layout.wall_cells:
            self.assertNotIn(wall, base)
        self.assertNotIn(layout.control_point, base)
        return layout

    def test_front_east(self):
        layout = self.check_layout((10, 24), "E")
        # 期望（锚点 xmin=10, ymin=23）：炮台 (12,24),(12,22),(11,22)，CP (12,23)
        self.assertEqual(set(layout.turret_cells), {Pos(12, 24), Pos(12, 22), Pos(11, 22)})
        self.assertEqual(layout.control_point, Pos(12, 23))

    def test_front_west(self):
        layout = self.check_layout((30, 7), "W")
        # 锚点 (30,6)：炮台 (29,7),(29,5),(30,5)，CP (29,6)
        self.assertEqual(set(layout.turret_cells), {Pos(29, 7), Pos(29, 5), Pos(30, 5)})
        self.assertEqual(layout.control_point, Pos(29, 6))

    def test_front_north_south(self):
        self.check_layout((10, 24), "N")
        self.check_layout((10, 24), "S")

    def test_mine_blocks_cell(self):
        # 矿占住规范炮台位 → 布局器应避开
        turn = make_turn((10, 24), mines={(12, 24): "stone"})
        layout = compute_layout(turn, "E")
        self.assertIsNotNone(layout)
        self.assertNotIn(Pos(12, 24), layout.turret_cells)

    def test_wall_priority_front_first(self):
        """建造优先级：正面（迎敌侧=离 CP 最远）优先（实战复盘修正）。"""
        turn = make_turn((10, 24))
        layout = compute_layout(turn, "W")
        dists = [distance(w, layout.control_point) for w in layout.wall_cells]
        self.assertEqual(dists, sorted(dists, reverse=True))


if __name__ == "__main__":
    unittest.main()
