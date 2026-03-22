# -*- coding: utf-8 -*-

import os
import sys
import platform
from pathlib import Path

# =========================================================================
# [配置] 调试开关
# =========================================================================
# True  : 开启虚拟环境自动检查与接管 (默认)
# False : 强制关闭虚拟化逻辑 (用于排查环境问题，或手动管理环境时)
ENABLE_VENV_AUTO_CHECK = True
# =========================================================================
# [新增] 强制全链路 UTF-8 (解决 Windows 命令行/pip 读取中文报错问题)
# PYTHONUTF8=1 : 让 Python 3.7+ 忽略系统区域设置，强制使用 UTF-8 (PEP 540)
# PYTHONIOENCODING : 强制标准输入输出流的编码
# =========================================================================
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"

# --- 添加依赖库路径 ---
current_file_path = Path(__file__).resolve()
project_root = current_file_path.parent.parent  # 指向 install/ 目录
deps_path = project_root / "agent"

# 将 agent 目录加入 python 搜索路径
if deps_path.exists():
    sys.path.insert(0, str(deps_path))

from utils import mfaalog # 日志
from utils import venv_ops # 虚拟化

# 环境治理逻辑 (虚拟化 + 模式判断)
# -----------------------------
def get_env_mode():
    """
    判断当前运行模式
    返回: 'dev' (开发/源码) 或 'release' (发布)
    判据: requirements.txt 是否存在
    """
    req_file = project_root / "requirements.txt"
    if req_file.exists():
        return 'dev'
    return 'release'

# 获取当前模式
current_mode = get_env_mode()
# 虚拟环境接管逻辑 (仅在开发模式且非内嵌环境时触发)
# 这里保留我们之前讨论的逻辑
if current_mode == 'dev':
    # 1. 优先检查手动开关
    if not ENABLE_VENV_AUTO_CHECK:
        mfaalog.warning("⚠️ [调试模式] 虚拟环境自动接管已通过开关禁用 (ENABLE_VENV_AUTO_CHECK=False)")
    
    else:
        # 2. 正常逻辑：检查是不是 Windows 内嵌 Python 防止误判
        is_embedded = False
        if sys.platform == "win32":
            try:
                if project_root in Path(sys.executable).resolve().parents:
                    is_embedded = True
            except:
                pass
        
        if not is_embedded:
            mfaalog.info("开发模式: 启动虚拟环境管理...")
            venv_ops.ensure_venv(project_root)

# -----------------------------
# 1. 动态计算 RID (Runtime Identifier)
system_name = platform.system().lower()  # 'windows', 'linux', 'darwin'
proc_arch = platform.machine().lower()   # 'amd64', 'x86_64', 'aarch64', 'arm64'

# [修复2] 必须给 rid 一个默认值，或者补全 Windows 分支
rid = "win-x64" # 默认值，防崩溃

if system_name == 'windows':
    if 'arm64' in proc_arch:
        rid = "win-arm64"
    else:
        rid = "win-x64"
elif system_name == 'linux':
    rid = "linux-arm64" if 'aarch64' in proc_arch else "linux-x64"
elif system_name == 'darwin':
    rid = "osx-arm64" if 'arm64' in proc_arch else "osx-x64"

# 2. 拼接 Native 库路径
dll_path = project_root / "runtimes" / rid / "native"

if current_mode == 'release':
    # 【发布模式】：必须手动指定 DLL 路径
    # 因为发布包里没有 pip 安装库，只有 runtimes 文件夹里的裸 DLL
    dll_path = project_root / "runtimes" / rid / "native"
    mfaalog.info(f"发布模式: 强制注入 DLL 路径 -> {dll_path}")
    
    os.environ["MAAFW_BINARY_PATH"] = str(dll_path)
    if system_name == 'windows':
        os.environ["PATH"] = str(dll_path) + os.pathsep + os.environ["PATH"]

else:
    # 【开发模式】：绝对不要乱指路！
    # 开发环境下，Python 会自动去 venv/site-packages 里找 pip 安装好的最新 DLL
    # 如果这里强行指向 runtimes，就会导致"代码是新的，DLL 是旧的"版本冲突
    mfaalog.info("开发模式: 跳过 DLL 路径注入 (使用 Python 库自带 DLL)  | agent//utils//venv_ops.py的maafw版本需要手动指定与agent一致")

from maa.agent.agent_server import AgentServer
from maa.toolkit import Toolkit

# 如果你有自定义动作/识别，在这里导入
import action # action子文件夹:agent/action/__init__.py里声明的全部
import recognition
from utils.persistent_store import PersistentStore # Agent配置文件热备份
from utils.instance_resolver import resolve_account_id  # [新增] 实例探测器
import fishing_agent # 钓鱼~

def main():
    # 设置 stdout 为 utf-8 (防止中文乱码)
    if sys.version_info >= (3, 7):
        sys.stdout.reconfigure(encoding='utf-8') # type: ignore

    print(f"Agent 正在启动... 根目录: {project_root}")

    # =========================================================================
    # [核心变更] 启动时一次性解析实例与存档号
    # =========================================================================
    # 获取 socket_id (由 MaaFramework 传入)
    socket_id = sys.argv[-1] if len(sys.argv) >= 2 else ""
    # 去除可能的 "socket_id=" 前缀
    if socket_id.startswith("socket_id="):
        socket_id = socket_id.split("=", 1)[1]

    if socket_id:
        account_id = resolve_account_id(socket_id, project_root)
        if account_id != "0":
            PersistentStore.switch_account(account_id)
    # =========================================================================

    PersistentStore.load() 
    mfaalog.info("✅ [Agent] 存档/备份系统已就绪")

    # 1. 初始化 Toolkit (借鉴 B 项目)
    # AgentServer 模式下仅 set_log_dir 生效，其余被忽略（上游已知行为）
    Toolkit.init_option(str(project_root))

    # 2. 获取 socket_id (由 MaaFramework 传入)
    if len(sys.argv) < 2:
        print("错误: 未收到 socket_id 参数，请勿直接运行此脚本，需由 MAA 启动。")
        return
    
    # [调整] socket_id 已在上方提取，此处直接使用
    print(f"Socket ID: {socket_id}")

    # 3. 启动服务
    try:
        AgentServer.start_up(socket_id)
        mfaalog.info("AgentServer 已启动，等待指令...")
        AgentServer.join()
    except Exception as e:
        mfaalog.warning(f"Agent 运行发生异常: {e}")
    finally:
        AgentServer.shut_down()
        mfaalog.info("AgentServer 已关闭")

if __name__ == "__main__":
    main()
