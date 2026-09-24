"""issue IKI8KF 回归（2026-09-24，仅保留 **自进化任务 + 宝藏** 部分）。

- 宝藏：推断 prompt 必须写明地图边界（防越界坐标被丢弃）。
- 任务：API 合同需含 `oldest_era` 年代序、禁止 `head -c` 截断、要求翻页取全。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.task import TaskPlanner
from agent.planners.treasure import TreasurePlanner

config.HARDCODED_ASSIST = True


class TestTreasurePromptBounds(unittest.TestCase):
    def test_prompt_states_map_bounds(self):
        p = TreasurePlanner()
        p.observe("传闻：石门在东方")
        text = p.prompt(41, 32)
        self.assertIn("41×32", text)
        self.assertIn("[0,40]", text)
        self.assertIn("[0,31]", text)


class TestApiContract(unittest.TestCase):
    def test_contract_has_era_order_and_no_head(self):
        c = TaskPlanner._contract("api")
        self.assertIn("oldest_era", c)
        self.assertIn("旧石器", c)
        self.assertIn("明清", c)
        self.assertNotIn("head -c 4000", c, "不应建议截断（会漏记录）")
        self.assertIn("完整", c)


if __name__ == "__main__":
    unittest.main()
