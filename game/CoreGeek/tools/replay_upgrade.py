#!/usr/bin/env python3
"""离线复盘：用当前（新）升级策略重算历史对局的升级决策（开发调试用，不打包）。

用法：python tools/replay_upgrade.py <issue> <round> [<round> ...]
从 Gitee 日志的遥测 `u/g/t.day` 重建最小 Turn，跑 UpgradePlanner.plan，
打印"新策略在该局面会升哪些墙 / 备几个修复包"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analyze_issue_log import parse_records  # noqa: E402
from agent.planners.upgrade import UpgradePlanner, wall_target_level, WALL_MAX_HP  # noqa: E402
from agent.protocol import Pos  # noqa: E402

KIND = {"S": "station", "G": "gatling", "Q": "railgun", "R": "rocket",
        "W": "wall", "P": "pioneer", "w": "worker"}


class U:
    def __init__(self, uid, kind, x, y, hp, level, bag_len=0):
        self.unit_id = uid
        self.kind = kind
        self.pos = Pos(x, y)
        self.health = hp
        self.level = level
        self.backpack = ["?"] * bag_len  # 遥测只存数量，内容未知（近似）


class T:
    def __init__(self, ours, gold, day):
        self.ours = ours
        self.gold = gold
        self.day_index = day

    def station(self):
        return next((u for u in self.ours if u.kind == "station"), None)

    def weapons(self):
        return tuple(u for u in self.ours if u.kind in ("gatling", "railgun", "rocket"))

    def walls(self):
        return tuple(u for u in self.ours if u.kind == "wall")


def load(issue: str):
    p = Path(r"C:\Users\ZZL\AppData\Local\Temp\opencode") / ("gitee_%s.json" % issue)
    d = json.load(open(p, encoding="utf-8"))
    recs = parse_records(d["body"])
    for c in d["comments"]:
        recs.extend(parse_records(c))
    return {r["r"]: r for r in recs
            if isinstance(r, dict) and "_error" not in r and isinstance(r.get("r"), int)}


def replay(good, rn):
    r = good[rn]
    ours = [U(u[0], KIND.get(u[1], "?"), u[2], u[3], u[4], u[5], u[7] if len(u) > 7 else 0)
            for u in (r.get("u") or [])]
    gold = r.get("g") or 0
    day = (r.get("t") or {}).get("day") or 0
    turn = T(ours, gold, day)
    st = turn.station()
    if st is None:
        print("r%d 无基地" % rn)
        return
    anchor = (st.pos.x, st.pos.y - 1)
    front = "W"
    cp = Pos(st.pos.x - 1, st.pos.y - 1)
    ms = UpgradePlanner().plan(turn, cp=cp, front=front)
    walls = turn.walls()
    lv = {}
    for w in walls:
        lv[w.level] = lv.get(w.level, 0) + 1
    tgt3 = [w for w in walls if wall_target_level(w.pos, anchor, front) == 3]
    tgt3_l3 = sum(1 for w in tgt3 if w.level >= 3)
    print("r%d D%s gold=%d 基地(%d,%d) 墙%s | 目标正面墙 L3 %d/%d"
          % (rn, day, gold, st.pos.x, st.pos.y, lv, tgt3_l3, len(tgt3)))
    for m in ms[:14]:
        tgt = (m.target.x, m.target.y) if m.target is not None else "-"
        print("    pri%-2d %-6s %-24s qty%d tgt%s" % (m.priority, m.kind, m.voucher, m.qty, tgt))
    if len(ms) > 14:
        print("    ... 共 %d 条" % len(ms))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    good = load(sys.argv[1])
    for a in sys.argv[2:]:
        replay(good, int(a))


if __name__ == "__main__":
    main()
