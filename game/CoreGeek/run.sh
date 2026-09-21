#!/usr/bin/env bash
# 兜底启动脚本：接口文档样例为 bash run.sh port
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python)"
exec "$PY" main3.py "$1"
