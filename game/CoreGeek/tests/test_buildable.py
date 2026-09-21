"""L2 BuildableMap 反馈学习单元测试。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos, Turn
from agent.planners.layout import compute_layout


class TestBuildableMap(unittest.TestCase):
    def test_illegal_cell_learned_and_avoided(self):
        # (12,24) 是规范炮台位；模拟该格建造非法（黄蓝区限制）
        sim = SimWorld(mines={(6, 22): "stone"}, illegal_builds={(12, 24)})
        brain = Brain()
        # 跑若干回合让 brain 尝试在 (12,24) 建火箭并收到失败反馈
        for _ in range(8):
            payload = sim.payload()
            response, _ = brain.decide(payload)
            sim.apply(response)
            sim.advance()
        # 应已记录 (12,24) 武器建造非法
        self.assertFalse(brain.buildable.is_usable(Pos(12, 24), "weapon"))
        # 布局重算后炮台不再包含该格
        self.assertNotIn(Pos(12, 24), brain.layout.turret_cells)
        # 且最终 3 座火箭仍能全部建成（备选格生效）
        self.assertEqual(len(sim.weapons()), 3)


if __name__ == "__main__":
    unittest.main()
