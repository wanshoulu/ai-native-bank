#!/bin/bash

# 使用 uv 管理环境与依赖，无需手动激活虚拟环境
# 首次运行请先执行: uv sync --no-dev

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 后台启动服务，日志输出到 logs/server.log
mkdir -p logs
nohup uv run --no-dev server.py > logs/server.log 2>&1 &

echo "服务已后台启动 (PID: $!)"
echo "日志: tail -f logs/server.log"
