"""issue IKIF4V 参考实现回归（2026-09-24）。

参考强队的宝藏实现，借鉴：
1. 任务用品**随地图变化**（任务书 §4.6.3）→ 从武器商店清单动态识别（`offerings_from_shop`）；
2. prompt 注入**地图中立元素坐标**（矿/小贩/商店/任务点）→ 方位词可锚定（`zones_text`）；
3. 传闻按天标注（`第N天传闻：...`）；
4. **新传闻 → 重推**（即使已有 ready 计划，未召唤前允许更新）。
"""
import unittest

import _bootstrap  # noqa: F401

from agent import config
from agent.planners.treasure import (
    NON_OFFERING,
    TREASURE_ITEMS,
    TreasurePlanner,
    offerings_from_shop,
    zones_text,
)
from agent.protocol import Turn
from harness import SimWorld

config.HARDCODED_ASSIST = True


class TestOfferingsFromShop(unittest.TestCase):
    def test_detects_map_specific_offerings(self):
        shop = ["WeaponUpgradeVoucher1", "WallFixer", "Bomb", "Medicine",
                "AcientTablet", "StarSand", "MysticRelic"]
        got = offerings_from_shop(shop)
        self.assertIn("AcientTablet", got)
        self.assertIn("MysticRelic", got)
        for fixed in ("Bomb", "WallFixer", "Medicine", "WeaponUpgradeVoucher1"):
            self.assertNotIn(fixed, got)

    def test_falls_back_when_shop_missing(self):
        self.assertEqual(offerings_from_shop([]), TREASURE_ITEMS)
        self.assertEqual(offerings_from_shop(None), TREASURE_ITEMS)

    def test_non_offering_covers_fixed_items(self):
        for name in ("Bomb", "DizzyWeapon", "WallFixer", "Medicine",
                     "SmallRobotSummonOrder", "BossRobotSummonOrder"):
            self.assertIn(name, NON_OFFERING)


class TestApplyLlmDynamicOfferings(unittest.TestCase):
    def test_map_specific_item_accepted_when_offered(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门，门需一钥")
        self.assertTrue(tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["MysticRelic"], "day": 4, "ready": true}',
            current_day=1, offerings=("MysticRelic", "StarSand")))
        self.assertEqual(tp.plan.items, ("MysticRelic",))

    def test_map_specific_item_rejected_by_default(self):
        tp = TreasurePlanner()
        tp.observe("传闻：西部有一石门")
        self.assertFalse(tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["MysticRelic"], "day": 4, "ready": true}'))


class TestPromptBorrowings(unittest.TestCase):
    def _turn(self):
        sim = SimWorld(station_pos=(10, 24), mines={(6, 22): "stone", (8, 20): "copper"})
        return Turn.load(sim.payload())

    def test_zones_text_lists_landmarks(self):
        text = zones_text(self._turn())
        self.assertIn("石矿(6,22)", text)
        self.assertIn("铜矿(8,20)", text)
        self.assertIn("武器商店(25,20)", text)

    def test_prompt_includes_zones_day_labels_and_offerings(self):
        tp = TreasurePlanner()
        tp.observe("西部有一石门，门需三钥", day=2)
        tp.observe("南边渡口的水位下降了", day=3)
        text = tp.prompt(41, 32, 3, zones="石矿(6,22)", offerings=("MysticRelic",))
        self.assertIn("石矿(6,22)", text)
        self.assertIn("第2天传闻：西部有一石门，门需三钥", text)
        self.assertIn("第3天传闻：南边渡口的水位下降了", text)
        self.assertIn("MysticRelic", text)


class TestReinferOnNewLegend(unittest.TestCase):
    def test_ready_plan_reinfers_on_new_legend(self):
        tp = TreasurePlanner()
        tp.observe("西部有一石门", day=1)
        self.assertTrue(tp.apply_llm(
            '{"x": 3, "y": 3, "items": ["AcientTablet"], "day": 5, "ready": true}',
            current_day=1))
        tp.mark_inferred()
        self.assertFalse(tp.needs_inference(), "同批线索不重推")
        tp.observe("南边渡口的水位下降了", day=2)
        self.assertTrue(tp.needs_inference(), "新传闻应允许重推（即使已有 ready 计划）")

    def test_no_reinfer_after_attempt(self):
        tp = TreasurePlanner()
        tp.observe("西部有一石门", day=1)
        tp.record_attempt()
        tp.observe("新线索", day=2)
        self.assertFalse(tp.needs_inference(), "已召唤后不再重推")


if __name__ == "__main__":
    unittest.main()