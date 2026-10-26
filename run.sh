#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "📦 正在初始化 Python 虚拟环境..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt
fi

echo "🚀 正在启动 沉浸式翻译 · 智能语境与术语网关..."
exec .venv/bin/python3 -m uvicorn gateway.server:app --host 127.0.0.1 --port 8787 --reload
