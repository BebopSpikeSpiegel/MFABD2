#!/bin/bash

# =======================================================
# 1. 解决路径问题：锁定在脚本所在的绝对目录（即程序根目录）
# =======================================================
CURRENT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$CURRENT_DIR"

# === 界面美化 ===
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# =======================================================
# 自检：本脚本自身有无隔离标记或缺少执行权限
# =======================================================
SELF="${BASH_SOURCE[0]}"
_SELF_WARN=false
xattr -p com.apple.quarantine "$SELF" &>/dev/null && _SELF_WARN=true
[ ! -x "$SELF" ] && _SELF_WARN=true

if $_SELF_WARN; then
    echo -e "${YELLOW}⚠️  本脚本自身存在限制，直接双击将被系统拦截${NC}"
    echo -e "   您当前可能是通过终端 bash 命令运行本脚本的。"
    echo -e "   若要使双击正常打开，请在终端执行：\n"
    xattr -p com.apple.quarantine "$SELF" &>/dev/null && \
        echo -e "   ${GREEN}xattr -d com.apple.quarantine \"$SELF\"${NC}"
    [ ! -x "$SELF" ] && \
        echo -e "   ${GREEN}chmod +x \"$SELF\"${NC}"
    echo ""
fi

echo -e "${BLUE}==========================================${NC}"
echo -e "${BLUE}   MFABD2 macOS 环境解锁与修复工具${NC}"
echo -e "${BLUE}==========================================${NC}"
echo -e "当前工作目录：$CURRENT_DIR"
echo -e "\n${YELLOW}⚠️  注意：解除 macOS 系统限制需要管理员权限，接下来可能会要求您输入开机密码。${NC}"
echo -e "${YELLOW}   （输入密码时屏幕不会显示字符，输入完成后直接按回车即可）${NC}"

# =======================================================
# 2. 融合 fix_mac 逻辑：移除隔离属性 (Gatekeeper)
# =======================================================
echo -e "\n${GREEN}>>> [1/3] 正在解除 macOS 隔离属性...${NC}"

# 结合原版 fix_mac 和用户的方案：递归移除当前目录下的隔离属性
sudo xattr -r -d com.apple.quarantine "$CURRENT_DIR" 2>/dev/null
# 备用保险：清理所有扩展属性
sudo xattr -cr "$CURRENT_DIR" 2>/dev/null

echo "✅ 隔离属性解除完成"

# =======================================================
# 3. 融合用户逻辑：赋予核心文件执行权限 (+x)
# =======================================================
echo -e "\n${GREEN}>>> [2/3] 正在修复核心组件执行权限...${NC}"

# A. 修复主程序 (MFAAvalonia)
if [ -f "$CURRENT_DIR/MFAAvalonia" ]; then
    sudo chmod +x "$CURRENT_DIR/MFAAvalonia"
    echo "✅ 主程序 (MFAAvalonia) 执行权限已修复"
else
    echo -e "${YELLOW}⚠️  未找到主程序 MFAAvalonia，请确认解压是否完整。${NC}"
fi

# B. 修复内嵌 Python (龟缩流的核心！必须给它权限，否则主程序调不动)
if [ -f "$CURRENT_DIR/python/bin/python3" ]; then
    sudo chmod +x "$CURRENT_DIR/python/bin/python3"
    echo "✅ 内嵌 Python 环境执行权限已修复"
else
    echo -e "${YELLOW}⚠️  未找到内嵌 Python (python/bin/python3)，程序可能会去寻找系统 Python。${NC}"
fi

# C. 修复所有同级辅助脚本
sudo chmod +x "$CURRENT_DIR"/*.sh "$CURRENT_DIR"/*.command 2>/dev/null
echo "✅ 附属脚本执行权限已修复"

# =======================================================
# 4. 修复内嵌 Python 缺失的原生库 (numpy/Pillow 的 .dylibs)
# =======================================================
echo -e "\n${GREEN}>>> [3/4] 检查 Python 原生库依赖...${NC}"

PY_BIN="$CURRENT_DIR/python/bin/python3"
if [ -f "$PY_BIN" ]; then
    # 检测是否存在缺失的 @loader_path dylib
    MISSING_PACKAGES=$("$PY_BIN" -c "
import os, subprocess, re, sysconfig, importlib.metadata
from pathlib import Path
site = Path(sysconfig.get_path('purelib'))
if not site.exists():
    exit()

DIS_NAME = {
    'PIL': 'Pillow',
    'cv2': 'opencv-python-headless',
}
try:
    for dist, names in importlib.metadata.packages_distributions().items():
        for n in names:
            DIS_NAME.setdefault(n, dist)
except Exception:
    pass

def resolve_dist(pkg):
    for k, v in DIS_NAME.items():
        if k.lower() == pkg.lower():
            return v
    return pkg

broken = set()
for so in site.rglob('*.so'):
    try:
        out = subprocess.check_output(['otool', '-L', str(so)], text=True)
    except Exception:
        continue
    for line in out.split('\n'):
        m = re.match(r'\s+(@loader_path/\S+)', line)
        if not m:
            continue
        ref = m.group(1)
        if not (so.parent / ref.replace('@loader_path/', '')).resolve().exists():
            broken.add(so.relative_to(site).parts[0])
for pkg in sorted(broken):
    dist_name = resolve_dist(pkg)
    try:
        print(f'{dist_name}=={importlib.metadata.version(dist_name)}')
    except Exception:
        print(dist_name)
" 2>/dev/null)

    if [ -n "$MISSING_PACKAGES" ]; then
        echo -e "${YELLOW}⚠️  检测到 Python 包原生库缺失，尝试联网修复...${NC}"
        echo "缺失的包:"
        echo "$MISSING_PACKAGES" | while read pkg_ver; do
            echo "  - $pkg_ver"
        done
        echo "正在修复中..."
        FAILED=0
        echo "$MISSING_PACKAGES" | while read pkg_ver; do
            if [ -n "$pkg_ver" ]; then
                if "$PY_BIN" -m pip install --force-reinstall --no-cache-dir "$pkg_ver" 2>/dev/null; then
                    echo "  ✅ $pkg_ver"
                else
                    echo "  ❌ $pkg_ver 修复失败"
                fi
            fi
        done
        echo -e "${GREEN}✅ Python 库修复完成${NC}"
    else
        echo "✅ Python 原生库依赖完整"
    fi
else
    echo -e "${YELLOW}⚠️  未找到内嵌 Python，跳过库检查${NC}"
fi

# =======================================================
# 5. 保留上游逻辑：安装 .NET 依赖
# =======================================================
echo -e "\n${GREEN}>>> [4/4] 检查并配置 .NET 环境...${NC}"
DOTNET_SCRIPT="DependencySetup_依赖库安装_mac.sh"

if [ -f "$CURRENT_DIR/$DOTNET_SCRIPT" ]; then
    bash "$CURRENT_DIR/$DOTNET_SCRIPT"
else
    echo -e "${YELLOW}⚠️  未找到 $DOTNET_SCRIPT，跳过 .NET 配置${NC}"
fi

# =======================================================
# 结束总结
# =======================================================
echo -e "\n${BLUE}==========================================${NC}"
echo -e "${GREEN}🎉 修复全部完成！${NC}"
echo -e "环境已由官方内置，无需联网下载。"
echo -e "请关闭此窗口，然后直接双击运行 MFAAvalonia。"
echo -e "${BLUE}==========================================${NC}"

# 保持窗口不关闭，让用户看清日志
read -p "按回车键退出..."
