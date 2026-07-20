#!/usr/bin/env bash
# =============================================================================
# overlay_core.sh —— MFA 内核【急救覆盖】共用例程
#
# 用途：把一份指定版本的 MaaFramework Core（release 解压后的目录）原地覆盖到
#       MFAAvalonia 自带内核所在目录（libMaaFramework.* 所在处），实现“同路径同名
#       覆盖”。meta 版本探测 与 桌面端出包 两处共用此脚本，保证行为一致 (DRY)。
#
# 用法：overlay_core.sh <core_src_dir> <target_root>
#   <core_src_dir> : MaaFramework Core 包解压后的根目录（内含 bin/ share/ ...）
#   <target_root>  : 要被覆盖的根（探测时是 temp_detect，出包时是 install）
#
# 行为：
#   1. 在 target_root 下定位 MFAA 自带的 libMaaFramework.*（.so/.dylib）或
#      MaaFramework.dll，取其所在目录为覆盖目标 NATIVE_DIR（可能有多个，逐个覆盖）。
#   2. 在 core_src_dir 下定位 Core 的 bin/ 目录，将 bin/* 覆盖进每个 NATIVE_DIR。
#   3. 若 Core 带 MaaAgentBinary（多在 share/ 下），同步覆盖 target_root 里已有的
#      MaaAgentBinary 目录（agent 协议须与内核同版本）。
#
# 只覆盖不清理：cp -rf 会盖掉/新增文件，但不删 MFAA 目录里多余的旧文件。对版本一致性
# 无害（真正的 MaaFramework 库+依赖都会被替成新版，按 soname 链接）。
#
# 成功后向 stdout 打印一行 “OVERLAY_NATIVE_DIR=<第一个覆盖目标目录>”，供调用方
# （如探测步骤）拿去读取覆盖后的版本。
# =============================================================================
set -euo pipefail

CORE_SRC="${1:?用法: overlay_core.sh <core_src_dir> <target_root>}"
TARGET_ROOT="${2:?用法: overlay_core.sh <core_src_dir> <target_root>}"

# 1) 定位 Core 的 bin 目录（以 libMaaFramework 为锚，兼容 .so/.dylib/.dll）
CORE_LIB=$(find "$CORE_SRC" \( -name 'libMaaFramework.*' -o -name 'MaaFramework.dll' \) | head -n 1 || true)
if [ -z "$CORE_LIB" ]; then
  echo "::error::覆盖内核源里找不到 libMaaFramework.*（$CORE_SRC）"
  exit 1
fi
CORE_BIN=$(dirname "$CORE_LIB")
echo "📦 覆盖内核源: $CORE_BIN"

# 2) 定位 target_root 里 MFAA 自带内核目录（可能多个 rid）
mapfile -t TARGET_LIBS < <(find "$TARGET_ROOT" \( -name 'libMaaFramework.*' -o -name 'MaaFramework.dll' \) || true)
if [ "${#TARGET_LIBS[@]}" -eq 0 ]; then
  echo "::error::目标里找不到 MFAA 自带内核（$TARGET_ROOT）"
  exit 1
fi

FIRST_NATIVE_DIR=""
declare -A SEEN_DIR=()
for lib in "${TARGET_LIBS[@]}"; do
  ndir=$(dirname "$lib")
  [ -n "${SEEN_DIR[$ndir]:-}" ] && continue
  SEEN_DIR[$ndir]=1
  echo "⚙️  覆盖内核: $CORE_BIN/* -> $ndir/"
  cp -rf "$CORE_BIN"/. "$ndir"/
  [ -z "$FIRST_NATIVE_DIR" ] && FIRST_NATIVE_DIR="$ndir"
done

# 3) 同步 MaaAgentBinary（若 Core 带、且目标里已有）
AB_SRC=$(find "$CORE_SRC" -type d -name 'MaaAgentBinary' | head -n 1 || true)
if [ -n "$AB_SRC" ]; then
  while IFS= read -r ab_dst; do
    [ -z "$ab_dst" ] && continue
    echo "⚙️  覆盖 MaaAgentBinary: $AB_SRC/* -> $ab_dst/"
    cp -rf "$AB_SRC"/. "$ab_dst"/
  done < <(find "$TARGET_ROOT" -type d -name 'MaaAgentBinary' || true)
fi

echo "OVERLAY_NATIVE_DIR=$FIRST_NATIVE_DIR"
