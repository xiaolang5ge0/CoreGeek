"""三个顺手项回归（2026-10-09）：
1. upgrade.py 派单 mission 价格对齐 v2.0 官方价：WallFixer cost 10→15、Bomb 100→50。
   （实际购买量由 _stock_qty 按 shop_prices 运行时价计算；cost 修订保证 mission 数据
    与官方价一致，防未来预算逻辑误用旧价。）
2. brain errorCode=5（LLM 超限）→ 当日封禁非任务 LLM（防连续超限，U18 保守口径）；
   跨天自动解封。
3. rules.py 旧版 _check_use 死代码已删（仅保留召唤令版）——用行为断言守护不回归。
"""
import unittest

import _bootstrap  # noqa: F401

from agent.brain import Brain
from agent.planners.upgrade import UpgradePlanner
from agent.protocol import Turn
from agent.rules import LegalityGuard
from test_v2_rules import base_payload

# 正则无法确定性解析的消息（无矿种/停工词）→ 必走 LLM 兜底路径
CRYPTIC_NEWS = "北方森林深处传出低语，有旅人称见到古老的祭坛石门泛起微光"


class TestPriceRevision(unittest.TestCase):
    """WallFixer mission cost = 15（v2.0 官方价）；Bomb = 50。"""

    def _plan_stock(self, gold: int, day_round: int = 300):
        payload = base_payload(day_round)
        payload["teamOur"]["goldNum"] = gold
        turn = Turn.load(payload)
        missions = UpgradePlanner().plan(turn)
        return missions

    def test_wallfixer_mission_cost_is_v20_price(self):
        missions = self._plan_stock(200)
        stock = [m for m in missions if m.voucher == "WallFixer"]
        self.assertTrue(stock, "D3+ 金币充足应派 WallFixer 备货")
        self.assertEqual(stock[0].cost, 15, "mission cost 应为 v2.0 官方价 15（原 10 已废）")

    def test_bomb_mission_cost_is_v20_price(self):
        # D6+（round 5*130+40=690）、武器墙全 L3、金币 300：RICH_GOLD(250)+50=300 恰好放行
        payload = base_payload(690)
        payload["teamOur"]["goldNum"] = 300
        # 三座满级火箭（kind rocket level3）让 all_weapons_l3 / front_ok 成立
        payload["teamOur"]["roles"].append(
            {"id": 10020, "pos": {"x": 11, "y": 25}, "roleType": "rocket",
             "health": 2000, "attackPower": 20, "attackRange": 10, "level": 3,
             "cooldown": 0, "isDriving": False}
        )
        payload["teamOur"]["roles"].append(
            {"id": 10021, "pos": {"x": 9, "y": 25}, "roleType": "rocket",
             "health": 2000, "attackPower": 20, "attackRange": 10, "level": 3,
             "cooldown": 0, "isDriving": False}
        )
        payload["teamOur"]["roles"].append(
            {"id": 10022, "pos": {"x": 10, "y": 23}, "roleType": "rocket",
             "health": 2000, "attackPower": 20, "attackRange": 10, "level": 3,
             "cooldown": 0, "isDriving": False}
        )
        turn = Turn.load(payload)
        missions = UpgradePlanner().plan(turn)
        bomb = [m for m in missions if m.voucher == "Bomb"]
        self.assertTrue(bomb, "D6+ 富余时应派炸弹备货")
        self.assertEqual(bomb[0].cost, 50, "mission cost 应为 v2.0 官方价 50（原 100 已废）")


class TestLLMBanOnErrorCode5(unittest.TestCase):
    """errorCode=5 → trace 记录 + 当日封禁非任务 LLM；跨天自动解封。"""

    def test_error5_bans_non_task_llm_today(self):
        brain = Brain()
        # 第 1 回合：不可解析消息（无 errorCode）→ 正常发 LLM 兜底 prompt
        p1 = base_payload(40)
        p1["worldNews"]["officialNews"] = CRYPTIC_NEWS
        r1, t1 = brain.decide(p1)
        self.assertTrue((r1.get("prompt") or "").strip(), "未封禁时不可解析消息应发 LLM prompt")
        # 第 2 回合：收到 errorCode=5 → 封禁记 trace
        p2 = base_payload(41)
        p2["errors"] = [{"errorCode": 5, "description": "LLM 调用次数超限"}]
        p2["worldNews"]["officialNews"] = CRYPTIC_NEWS + "，另一则见闻"
        r2, t2 = brain.decide(p2)
        self.assertIn("llm_ban", t2, "errorCode=5 应记录当日封禁")
        # 第 3 回合：同日再遇不可解析消息 → 已封禁，不得再发
        p3 = base_payload(42)
        p3["worldNews"]["officialNews"] = CRYPTIC_NEWS + "，第三则见闻"
        p3["worldNews"]["folkLegends"] = "有人说祭坛在东南方"
        r3, t3 = brain.decide(p3)
        self.assertEqual(r3.get("prompt") or "", "", "封禁当日不得再发非任务 prompt")

    def test_ban_lifts_next_day(self):
        brain = Brain()
        p1 = base_payload(40)
        p1["errors"] = [{"errorCode": 5, "description": "超限"}]
        brain.decide(p1)
        # 次日 Day2（round 140）：封禁自动解除，不可解析消息应正常发 prompt
        p2 = base_payload(140)
        p2["worldNews"]["officialNews"] = CRYPTIC_NEWS
        r2, t2 = brain.decide(p2)
        self.assertNotIn("llm_ban", t2, "跨天应解封")
        self.assertTrue((r2.get("prompt") or "").strip(), "解封后不可解析消息应发 LLM prompt")

    def test_other_error_codes_do_not_ban(self):
        brain = Brain()
        p1 = base_payload(40)
        p1["errors"] = [{"errorCode": 2, "description": "答案错误"}]
        p1["worldNews"]["officialNews"] = CRYPTIC_NEWS
        r1, t1 = brain.decide(p1)
        self.assertNotIn("llm_ban", t1, "errorCode=2 不触发 LLM 封禁")
        self.assertTrue((r1.get("prompt") or "").strip(), "非 5 错误码不影响 LLM")


class TestUseRulesSurviveDedup(unittest.TestCase):
    """rules.py 删除旧版 _check_use 后，新版（含召唤令分支）是唯一实现且行为正确。"""

    def test_use_dizzy_without_targetpos_illegal(self):
        payload = base_payload()
        payload["teamOur"]["roles"][1]["backpack"] = ["DizzyWeapon"]
        turn = Turn.load(payload)
        verdict = LegalityGuard(turn).check(10010, {
            "action": "use", "name": "DizzyWeapon",
        })
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason, "bad_targetPos")

    def test_use_plain_item_legal(self):
        # 非目标道具（如生命药剂）无需 targetPos，直接合法（走新版 _check_use 尾部 return True）
        payload = base_payload()
        payload["teamOur"]["roles"][1]["backpack"] = ["Medicine"]
        turn = Turn.load(payload)
        verdict = LegalityGuard(turn).check(10010, {"action": "use", "name": "Medicine"})
        self.assertTrue(verdict.ok)


if __name__ == "__main__":
    unittest.main()
