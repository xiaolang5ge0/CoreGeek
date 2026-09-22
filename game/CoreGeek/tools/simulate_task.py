#!/usr/bin/env python3
"""任务流场景仿真 v3（开发调试用，不打包）：WS 工程修复类，健壮探索→探测→TOKEN 提交。

用法：python tools/simulate_task.py [rounds]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from harness import SimWorld  # noqa: E402
from agent.brain import Brain  # noqa: E402

TASK = {
    "pos": (14, 14),
    "text": "任务：修复 ws_3 工程，通过 ./check",
    "scoreReward": 50,
    "goldReward": 30,
    "timeoutRounds": 60,
}

ENG_EXPLORE = (
    "[exitCode:0]\n"
    "__FILE:/tmp/selfEvolutionTask/ws_3/task_ws3.md\n"
    "=== TASK ===\n任务：修复 ws_3 工程，通过 ./check\n"
    "=== FILE:/tmp/selfEvolutionTask/ws_3/spec.md ===\n目录 data 权限 755\n"
    "=== LIST ===\n/tmp/selfEvolutionTask/ws_3/check 755\n"
    "__DIR:/tmp/selfEvolutionTask/ws_3\n"
)


def ws_handler(cmd: str) -> str:
    if "find /tmp/selfEvolutionTask" in cmd:
        return ENG_EXPLORE
    if "-maxdepth 3 -type f -name check" in cmd:
        # 模拟工作区已就绪：./check 通过并输出 TOKEN → 确定性提交（零 LLM）
        return "[exitCode:0]\n__WS:/tmp/selfEvolutionTask/ws_3\n[ OK ] 全部通过\nTOKEN: abc123token\n"
    return "[exitCode:0]\n"


WATCH = {1, 11, 14, 30, 55, 70, 71, 90, 140, 200, 260}


def main() -> None:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 260
    sim = SimWorld(
        station_pos=(10, 24),
        mines={(6, 22): "stone", (8, 20): "copper", (14, 6): "iron"},
        tasks=[TASK],
        cmd_handler=ws_handler,
        expected_answer="abc123token",
    )
    brain = Brain()
    respawns = [(8, 20, "copper"), (16, 14, "copper"), (6, 22, "stone"), (9, 19, "copper")]
    for r in range(1, rounds + 1):
        if not sim.mines and respawns:
            x, y, kind = respawns.pop(0)
            sim.add_mine((x, y), kind)
        if r in (71, 201):
            for i, (x, y) in enumerate([(16, 23), (17, 24), (16, 22), (18, 23), (17, 22)]):
                sim.spawn_robot(x, y, "smallRobot", hp=40, rid=30010 + i)
        response, trace = brain.decide(sim.payload())
        if r in WATCH:
            acts = {rid: c.get("action") for rid, c in response["roleCommandMap"].items()}
            pioneer = trace.get("pioneer", {})
            print(
                f"r={r:3d} gold={sim.gold:3d} score={sim.score} acts={acts} "
                f"pioneer={pioneer.get('state')}"
            )
        sim.apply(response)
        sim.advance()
    print("---- 结果 ----")
    print("任务得分:", sim.score, "| 金币:", sim.gold, "| LLM 调用:", len(sim.prompts_seen))
    print("提交:", sim.submissions)
    print("围墙:", len(sim.walls()), "| 武器:", [(w["roleType"], w["level"]) for w in sim.weapons()])
    print("开拓者:", sim.role(10011)["pos"], "| 状态机:", brain.pioneer_fsm.state)


if __name__ == "__main__":
    main()

