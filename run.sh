#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Windows(Git Bash) 的 venv 在 Scripts/，Linux/macOS 在 bin/
if [ -x ".venv/Scripts/python.exe" ]; then
    VENV_PY=".venv/Scripts/python.exe"
else
    VENV_PY=".venv/bin/python3"
fi

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 正在初始化 Python 虚拟环境..."
    BASE_PY="$(command -v python3 || command -v python)"
    "$BASE_PY" -m venv .venv
    if [ -x ".venv/Scripts/python.exe" ]; then
        VENV_PY=".venv/Scripts/python.exe"
        "$VENV_PY" -m pip install --upgrade pip
        "$VENV_PY" -m pip install -r requirements.txt
    else
        VENV_PY=".venv/bin/python3"
        "$VENV_PY" -m pip install --upgrade pip
        "$VENV_PY" -m pip install -r requirements.txt
    fi
fi

# 避免 Windows GBK 控制台在打印 emoji 时崩溃
export PYTHONIOENCODING="${PYTHONIOENCODING:-utf-8}"

echo "🚀 正在启动 沉浸式翻译 · 智能语境与术语网关..."
exec "$VENV_PY" -m uvicorn gateway.server:app --host 127.0.0.1 --port 8787 --reload
