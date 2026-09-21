"""L2 BuildableMap 反馈学习单元测试。"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos


class TestBuildableMap(unittest.TestCase):
    def test_illegal_cell_learned_and_avoided(self):
        # (9,24) 是当前布局的规范炮台位；模拟该格建造非法（实战建造区偏差情形）
        sim = SimWorld(mines={(6, 22): "stone"}, illegal_builds={(9, 24)})
        brain = Brain()
        # 跑若干回合：brain 尝试在 (9,24) 建火箭 → 失败反馈 → 拉黑 → 布局迁移
        for _ in range(12):
            payload = sim.payload()
            response, _ = brain.decide(payload)
            sim.apply(response)
            sim.advance()
        # 应已记录 (9,24) 武器建造非法
        self.assertFalse(brain.buildable.is_usable(Pos(9, 24), "weapon"))
        # 布局重算后炮台不再包含该格（CP 迁移）
        self.assertNotIn(Pos(9, 24), brain.layout.turret_cells)
        # 且最终 3 座火箭仍能全部建成（备选格生效）
        self.assertEqual(len(sim.weapons()), 3)


if __name__ == "__main__":
    unittest.main()
