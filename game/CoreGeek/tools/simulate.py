#!/usr/bin/env python3
"""本地场景仿真：Day1 + Night1 全流程（开发调试用，不打包）。

用法：python tools/simulate.py [rounds]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from harness import SimWorld  # noqa: E402
from agent.brain import Brain  # noqa: E402

WATCH = {1, 5, 10, 20, 30, 50, 69, 70, 71, 72, 75, 80, 100, 130, 140, 170, 200, 201, 230, 260}


RESPAWNS = [
    (8, 20, "copper"), (16, 14, "copper"), (6, 22, "stone"),
    (9, 19, "copper"), (20, 8, "iron"), (12, 12, "stone"),
]


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 130
    sim = SimWorld(
        station_pos=(10, 24),
        mines={(6, 22): "stone", (8, 20): "copper", (14, 6): "iron"},
    )
    brain = Brain()
    fired: list[int] = []
    sells = 0
    respawns = list(RESPAWNS)
    for r in range(1, rounds + 1):
        if not sim.mines and respawns:  # 模拟矿刷新
            x, y, kind = respawns.pop(0)
            sim.add_mine((x, y), kind)
        if r == 71 or r == 201:  # Night1/Night2 机器人从 FRONT(东) 来袭
            for i, (x, y) in enumerate([(16, 23), (17, 24), (16, 22), (18, 23), (17, 22)]):
                sim.spawn_robot(x, y, "smallRobot", hp=40, rid=30010 + i)
        response, trace = brain.decide(sim.payload())
        if r in WATCH:
            acts = {rid: c.get("action") for rid, c in response["roleCommandMap"].items()}
            print(
                f"r={r:3d} gold={sim.gold:3d} weapons={len(sim.weapons())} "
                f"walls={len(sim.walls()):2d} acts={acts} phase={trace.get('phase', '-')}"
            )
        for rid, cmd in response["roleCommandMap"].items():
            if cmd.get("action") == "attack":
                fired.append(int(rid))
            elif cmd.get("action") == "sell":
                sells += 1
        sim.apply(response)
        sim.advance()

    pioneer = sim.role(10011)
    w1, w2 = sim.role(10010), sim.role(10012)
    print("---- 结果 ----")
    print("pioneer pos:", pioneer["pos"], "(CP 应为 (12,23))")
    print("worker1 背包:", len(w1["backpack"]), w1["backpack"][:10])
    print("worker2 背包:", len(w2["backpack"]), w2["backpack"][:10])
    print("攻击次数:", len(fired), "使用武器:", sorted(set(fired)))
    print("卖货次数:", sells, "最终金币:", sim.gold)
    print("围墙:", sorted((w["pos"]["x"], w["pos"]["y"]) for w in sim.walls()))


if __name__ == "__main__":
    main()
