#!/usr/bin/env python3
"""一键打包：dist/CoreGeek.tar.gz。

tar 内以顶层目录 `CoreGeek/` 包裹（平台解包后运行 <root>/CoreGeek/main3.py，
实战报错证实该结构：python3: can't open file '/home/docker/CoreGeek/main3.py'）。
排除 tests/ logs/ dist/ __pycache__/ *.pyc 等无关文件。
"""
from __future__ import annotations

import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
TOP_DIR = "CoreGeek"  # tar 顶层目录名（与包名一致）
INCLUDE = ("main3.py", "run.sh", "src")
EXCLUDE_DIR_NAMES = {"__pycache__", ".idea"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXECUTABLES = {"run.sh"}


def _excluded(rel: Path) -> bool:
    return bool(set(rel.parts) & EXCLUDE_DIR_NAMES) or rel.suffix in EXCLUDE_SUFFIXES


def main() -> None:
    DIST.mkdir(exist_ok=True)
    target = DIST / "CoreGeek.tar.gz"
    count = 0
    with tarfile.open(target, "w:gz") as tar:
        for name in INCLUDE:
            base = ROOT / name
            if base.is_file():
                arcname = f"{TOP_DIR}/{name}"
                info = tar.gettarinfo(str(base), arcname=arcname)
                if name in EXECUTABLES:
                    info.mode = 0o755
                with open(base, "rb") as fh:
                    tar.addfile(info, fh)
                count += 1
            elif base.is_dir():
                for path in sorted(base.rglob("*")):
                    rel = path.relative_to(ROOT)
                    if path.is_file() and not _excluded(rel):
                        tar.add(path, arcname=f"{TOP_DIR}/{rel.as_posix()}")
                        count += 1
    print(f"built {target} ({target.stat().st_size} bytes, {count} files)")


if __name__ == "__main__":
    main()

