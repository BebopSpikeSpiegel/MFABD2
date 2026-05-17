#!/bin/bash
# mac_launcher.sh
# 每次启动 agent 时自动执行，解决更新后权限丢失与 quarantine 问题。
# 由 /bin/bash 直接读取执行，无需自身具备 +x 权限。

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$AGENT_DIR/.." && pwd)"
PY3="$ROOT_DIR/python/bin/python3"

# 1. 清除整个安装目录的 quarantine 标记（用户自有文件，无需 sudo）
xattr -rd com.apple.quarantine "$ROOT_DIR" 2>/dev/null

# 2. 修复内嵌 python3 的执行权限
chmod +x "$PY3" 2>/dev/null

# 3. 启动
exec "$PY3" -u "$AGENT_DIR/main.py"
