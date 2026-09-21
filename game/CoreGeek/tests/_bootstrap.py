"""测试公共引导：把 src 加入 sys.path，定位 fixture。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "request_sample.json"
