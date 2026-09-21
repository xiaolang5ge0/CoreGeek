#!/usr/bin/env python3
"""任务流场景仿真（开发调试用，不打包）。"""
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
    "text": "任务：调用本地天气接口，查询北京今日天气",
    "scoreReward": 50,
    "goldReward": 30,
    "timeoutRounds": 60,
}

LLM = [
    '{"analysis":"需要探索本地接口","commands":["ls /data","cat /data/api.txt"],"answer":null,"done":false}',
    '{"analysis":"已得天气","commands":[],"answer":{"city":"北京","weather":"晴"},"done":true}',
]

WATCH = {1, 12, 14, 16, 18, 20, 30, 69, 70, 71, 80}


def main() -> None:
    sim = SimWorld(
        station_pos=(10, 24),
        mines={(6, 22): "stone", (8, 20): "copper"},
        tasks=[TASK],
        llm_script=list(LLM),
        cmd_handler=lambda cmd: "[exitCode:0]\n北京 晴 25C",
        expected_answer="北京",
    )
    brain = Brain()
    for r in range(1, 91):
        if r == 71:
            for i, (x, y) in enumerate([(16, 23), (17, 24), (16, 22)]):
                sim.spawn_robot(x, y, "smallRobot", hp=40, rid=30010 + i)
        response, trace = brain.decide(sim.payload())
        if r in WATCH:
            acts = {rid: c.get("action") for rid, c in response["roleCommandMap"].items()}
            extra = ""
            if response.get("prompt"):
                extra = " PROMPT"
            if response.get("executeCmd"):
                extra += f" CMD[{response['executeCmd']}]"
            pioneer = trace.get("pioneer", {})
            print(
                f"r={r:3d} gold={sim.gold:3d} score={sim.score} acts={acts}"
                f" pioneer={pioneer.get('state')}{extra}"
            )
        sim.apply(response)
        sim.advance()
    print("---- 结果 ----")
    print("提交:", sim.submissions)
    print("得分:", sim.score, "金币:", sim.gold)
    print("开拓者:", sim.role(10011)["pos"], "状态机:", brain.pioneer_fsm.state)


if __name__ == "__main__":
    main()
