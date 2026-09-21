#!/usr/bin/env python3
"""《未来战争》入口：主办方扫描本文件加载。

用法：python main3.py <port>（监听 0.0.0.0:port）
"""
import logging
import os
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main3.py <port>")
    port = int(sys.argv[1])
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
    )

    from agent.server import serve

    logging.info("listening on 0.0.0.0:%d", port)
    serve(port)


if __name__ == "__main__":
    main()
