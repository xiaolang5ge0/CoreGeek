"""L5 遥测 v2：精简 + 加密（用户要求，密钥 12345678）。

- 精简：不再全量回显 request；角色/机器人压成短数组，zones 仅变化时输出，
  文本字段截断。保留定位问题所需的全部关键信息。
- 加密：SHA256(key+counter) 密钥流 XOR + base64（标准库实现，防直读）。
  本地分析用 tools/decrypt_log.py 解密。
- 平台只见 stdout：每回合打印一行 `round_record <b64>`；同时写 logs/*.jsonl.enc。
- 任何 IO 失败静默降级，绝不影响主决策。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

LOG_KEY = b"12345678"

_KIND_CH = {
    "station": "S", "gatling": "G", "railgun": "Q", "rocket": "R", "wall": "W",
    "pioneer": "P", "worker": "w",
    "smallRobot": "s", "middleRobot": "m", "largeRobot": "L", "bossRobot": "B",
}
_ZONE_CH = {
    "stone": "s", "iron": "i", "copper": "c", "vendor": "V", "weaponShop": "S",
}


def _keystream(n: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(LOG_KEY + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:n]


def encrypt_text(text: str) -> str:
    data = text.encode("utf-8")
    ks = _keystream(len(data))
    return base64.b64encode(bytes(a ^ b for a, b in zip(data, ks))).decode()


def compact_record(
    round_no: Any,
    request: Any,
    response: Any,
    trace: dict[str, Any],
    prev_zones_sig: list,
) -> dict:
    """request 精简为定位所需的最小字段集。"""
    rec: dict[str, Any] = {"r": round_no, "t": trace}
    resp = response or {}
    cmds = resp.get("roleCommandMap") or {}
    if cmds:
        compact_cmds = {}
        for k, v in cmds.items():
            targets = [[p.get("x"), p.get("y")] for p in (v.get("targetPos") or [])]
            entry = [v.get("action")]
            if v.get("name"):
                entry.append(v["name"])
            if targets:
                entry.append(targets)
            if v.get("num"):
                entry.append(v["num"])
            if v.get("controllerId"):
                entry.append("ctl" + str(v["controllerId"]))
            if v.get("taskAnswer"):
                entry.append(str(v["taskAnswer"])[:80])
            compact_cmds[str(int(k) % 1000)] = entry
        rec["c"] = compact_cmds
    if resp.get("prompt"):
        rec["prompt_len"] = len(resp["prompt"])
    if resp.get("executeCmd"):
        rec["xcmd"] = resp["executeCmd"][:200]

    if isinstance(request, dict):
        team = request.get("teamOur") or {}
        rec["g"] = team.get("goldNum")
        rec["sc"] = team.get("totalScore")
        rec["u"] = [
            [int(r.get("id", 0)) % 1000, _KIND_CH.get(r.get("roleType"), "?"),
             (r.get("pos") or {}).get("x"), (r.get("pos") or {}).get("y"),
             r.get("health"), r.get("level", 0), r.get("cooldown", 0),
             len(r.get("backpack") or [])]
            for r in team.get("roles") or []
        ]
        robots = (request.get("robot") or {}).get("roles") or []
        if len(robots) <= 40:
            rec["b"] = [
                [(r.get("pos") or {}).get("x"), (r.get("pos") or {}).get("y"),
                 _KIND_CH.get(r.get("roleType"), "?"), r.get("health")]
                for r in robots
            ]
        else:  # 大潮只留统计+质心，防止日志爆炸
            xs = [r["pos"]["x"] for r in robots if r.get("pos")]
            ys = [r["pos"]["y"] for r in robots if r.get("pos")]
            kinds: dict[str, int] = {}
            for r in robots:
                k = _KIND_CH.get(r.get("roleType"), "?")
                kinds[k] = kinds.get(k, 0) + 1
            rec["b"] = {"n": len(robots), "cx": sum(xs) // max(1, len(xs)),
                        "cy": sum(ys) // max(1, len(ys)), "k": kinds}
        zones = (request.get("mapInfo") or {}).get("zones") or []
        sig = hashlib.md5(json.dumps(zones, sort_keys=True).encode()).hexdigest()[:10]
        if sig != (prev_zones_sig[0] if prev_zones_sig else None):
            rec["z"] = [[_ZONE_CH.get(z.get("neutralType"), z.get("neutralType", "?")[:1]),
                         (z.get("pos") or {}).get("x"), (z.get("pos") or {}).get("y")]
                        for z in zones]
            prev_zones_sig[:] = [sig]
        fb = request.get("lastRoundRoleActionResults") or {}
        fails = [k for k, v in fb.items() if v is False]
        if fails:
            rec["fb_fail"] = fails
        errs = request.get("errors") or []
        if errs:
            rec["err"] = [[e.get("errorCode"), str(e.get("description"))[:40]] for e in errs]
        if request.get("phaseTask"):
            rec["pt"] = str(request["phaseTask"])[:120]
        if request.get("llmResp"):
            rec["llm"] = str(request["llmResp"])[:200]
        if request.get("lastCmdResult"):
            rec["lcr"] = str(request["lastCmdResult"])[:300]
    return rec


class Telemetry:
    def __init__(self, log_dir: str = "logs", enabled: bool = True):
        self._file = None
        self._zones_sig: list = []
        if not enabled:
            return
        try:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            name = time.strftime("match_%Y%m%d_%H%M%S") + ".jsonl.enc"
            self._file = open(path / name, "a", encoding="utf-8")
            LOGGER.info("telemetry(enc) -> %s", path / name)
        except Exception:
            LOGGER.warning("telemetry disabled: cannot open log file", exc_info=True)
            self._file = None

    def log_round(self, round_no: Any, request: Any, response: Any, trace: dict[str, Any]) -> None:
        try:
            rec = compact_record(round_no, request, response, trace, self._zones_sig)
            line = encrypt_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        except Exception:
            return
        try:
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()
        except Exception:
            pass
        try:
            LOGGER.info("round_record %s", line)
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
