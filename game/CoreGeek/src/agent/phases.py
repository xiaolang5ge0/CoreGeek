"""L3 StrategyPhaseManager：阶段判定（P1 仅 BOOTSTRAP/ECONOMY，P4+ 扩展 MIDGAME）。"""
from __future__ import annotations

from .protocol import Turn

BOOTSTRAP = "BOOTSTRAP"
ECONOMY = "ECONOMY"


class PhaseManager:
    def phase(self, turn: Turn, layout_complete: bool) -> str:
        if turn.day_index <= 1 and not layout_complete:
            return BOOTSTRAP
        return ECONOMY
