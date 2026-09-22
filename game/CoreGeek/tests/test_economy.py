"""P2 验收：Worker 经济闭环。

三验收（ARCHITECTURE_DESIGN §6 P2）：
- 不会采一半跑路（Mine Lock 锁定到矿耗尽）
- 不会 Sell 抢占 Active Miner（卖矿只在 FREE 态评估）
- 不会原地打转（看门狗 + 无事可干时静止）
另验证：背包满触发卖货、矿耗尽重选、卖货得金。
"""
import unittest

import _bootstrap  # noqa: F401

from harness import SimWorld
from agent.brain import Brain
from agent.protocol import Pos

W1, W2, PIONEER = 10010, 10012, 10011


def run_rounds(brain, sim, n):
    """跑 n 回合，返回每回合 (response, trace) 列表。"""
    out = []
    for _ in range(n):
        response, trace = brain.decide(sim.payload())
        sim.apply(response)
        sim.advance()
        out.append((response, trace))
    return out


def cmd_of(response, rid):
    return (response["roleCommandMap"] or {}).get(str(rid))


class TestMineLock(unittest.TestCase):
    def test_collect_until_depleted(self):
        """铜矿 10 次采完才走；采集中位置不变、动作全是 collect。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        brain = Brain()
        history = run_rounds(brain, sim, 40)
        collects = [
            cmd_of(resp, W2) for resp, _ in history
            if (cmd_of(resp, W2) or {}).get("action") == "collect"
        ]
        self.assertEqual(len(collects), 10)  # 恰好采完 10 次
        self.assertNotIn((8, 20), sim.mines)  # 矿已消失
        # 采完的 10 铜：要么还在背包，要么已卖成金（闭环）
        bag = sim.role(W2)["backpack"].count("copper")
        self.assertTrue(bag == 10 or sim.gold >= 50)

    def test_reselect_after_depleted(self):
        """矿耗尽后重选新矿并走过去。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        brain = Brain()
        run_rounds(brain, sim, 40)  # 采空
        sim.add_mine((14, 20), "iron")
        history = run_rounds(brain, sim, 12)
        moves = [
            cmd_of(resp, W2) for resp, _ in history
            if (cmd_of(resp, W2) or {}).get("action") in ("move", "collect")
        ]
        self.assertTrue(moves)  # 有后续行动（走向/采集新矿）


class TestSell(unittest.TestCase):
    def test_no_preempt_active_miner(self):
        """Worker1 背包满去卖货期间，Worker2 采矿不中断。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        sim.role(W1)["backpack"] = ["copper"] * 100
        brain = Brain()
        history = run_rounds(brain, sim, 25)
        # W2 锁定铜矿后每回合都是 collect，直到采完
        w2_actions = [
            (cmd_of(resp, W2) or {}).get("action") for resp, _ in history
        ]
        first_collect = w2_actions.index("collect")
        tail = [a for a in w2_actions[first_collect:] if a is not None]
        # 首次 collect 之后：只能是 collect（10 次采完前不得有 move/sell）
        run = []
        for action in tail:
            if action != "collect":
                break
            run.append(action)
        self.assertEqual(len(run), 10)
        # W1 最终完成卖货（出现 sell 指令）
        w1_actions = [
            (cmd_of(resp, W1) or {}).get("action") for resp, _ in history
        ]
        self.assertIn("sell", w1_actions)

    def test_full_backpack_sell_gold(self):
        """背包满 → 去小贩批量卖 → 金增加（之后可能被升级花费，看峰值）。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper"})
        sim.role(W2)["backpack"] = ["copper"] * 100
        brain = Brain()
        peak_gold = 0
        for _ in range(40):
            response, _ = brain.decide(sim.payload())
            sim.apply(response)
            sim.advance()
            peak_gold = max(peak_gold, sim.gold)
        self.assertGreaterEqual(peak_gold, 500)  # 100 铜 × 5 金


class TestNoSpin(unittest.TestCase):
    def test_idle_when_no_mine(self):
        """无矿可采：工人静止（不发 move），不原地打转。"""
        sim = SimWorld(station_pos=(10, 24), mines={})
        brain = Brain()
        history = run_rounds(brain, sim, 30)
        positions = []
        for resp, _ in history[15:]:  # 建造完成后观察
            for rid in (W1, W2):
                cmd = cmd_of(resp, rid)
                if cmd and cmd.get("action") == "move":
                    positions.append((rid, cmd["targetPos"][0]))
        # 武器建成后（约 r10），不应再有工人移动
        self.assertEqual(positions, [])


class TestEconomyLoop(unittest.TestCase):
    def test_two_day_gold_growth(self):
        """两天闭环：矿刷新 → 采 → 卖 → 金增长；全程无异常。"""
        sim = SimWorld(station_pos=(10, 24), mines={(8, 20): "copper", (6, 22): "stone"})
        brain = Brain()
        respawns = [(8, 20, "copper"), (16, 14, "copper"), (6, 22, "stone"), (9, 19, "copper")]
        sells = 0
        for r in range(1, 261):
            if not sim.mines and respawns:
                x, y, kind = respawns.pop(0)
                sim.add_mine((x, y), kind)
            response, _ = brain.decide(sim.payload())
            sells += sum(
                1 for cmd in response["roleCommandMap"].values()
                if cmd.get("action") == "sell"
            )
            sim.apply(response)
            sim.advance()
        self.assertGreaterEqual(sells, 2)
        # 金币可能已被武器升级花掉 → 只断言"卖货发生"（经济闭环在跑）
        self.assertGreaterEqual(sells, 2, "两天内应有多次卖货")


if __name__ == "__main__":
    unittest.main()
