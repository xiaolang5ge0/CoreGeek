"""L5 遥测：每回合结构化落盘完整 Request + Response + decision_trace（JSONL）。

用户硬性要求（STRATEGY_DECISIONS #17）：每回合打印完整 Request 与 roleCommandMap。
约束：任何 IO 失败静默降级，绝不影响主决策流程。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


class Telemetry:
    def __init__(self, log_dir: str = "logs", enabled: bool = True):
        self._file = None
        if not enabled:
            return
        try:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            name = time.strftime("match_%Y%m%d_%H%M%S") + ".jsonl"
            self._file = open(path / name, "a", encoding="utf-8")
            LOGGER.info("telemetry -> %s", path / name)
        except Exception:
            LOGGER.warning("telemetry disabled: cannot open log file", exc_info=True)
            self._file = None

    def log_round(
        self,
        round_no: Any,
        request: Any,
        response: Any,
        trace: dict[str, Any],
    ) -> None:
        record = {
            "roundNo": round_no,
            "request": request,
            "response": response,
            "trace": trace,
        }
        try:
            if self._file is not None:
                self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._file.flush()
        except Exception:
            pass  # 静默降级
        try:
            commands = (response or {}).get("roleCommandMap") or {}
            LOGGER.info(
                "round=%s day=%s isDay=%s gold=%s cmds=%s",
                round_no,
                trace.get("day", "?"),
                trace.get("is_day", "?"),
                trace.get("gold", "?"),
                {rid: c.get("action") for rid, c in commands.items()},
            )
        except Exception:
            pass

    def close(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except Exception:
            pass
        self._file = None
