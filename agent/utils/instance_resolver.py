# -*- coding: utf-8 -*-
"""
实例探测器 (Instance Resolver)
==============================
在 Agent 启动时一次性解析当前进程归属的实例与用户存档号。

探测链路 (按优先级):
  1. 环境变量 MFA_INSTANCE_ID  → 上游 MFAAvalonia 注入 (未来)
  2. 日志反查 socket_id        → 从 MFA 日志提取 [inst=.../instance_id]
  3. 回退默认值 "default"

拿到 instance_id 后:
  → 读取 config/instances/{instance_id}.json
  → 提取用户在「多存档」选项中填写的存档号
  → 返回给 PersistentStore.switch_account()
"""

import os
import re
import json
import glob
from pathlib import Path
from datetime import datetime

from . import mfaalog as logger

# =============================================================================
# 公开接口
# =============================================================================

def resolve_account_id(socket_id: str, project_root: Path) -> str:
    """
    【唯一外部调用入口】启动时一次性解析存档号。

    Parameters
    ----------
    socket_id : str
        AgentServer 握手标识符，即 sys.argv 传入的 socket_id。
    project_root : Path
        项目根目录 (install/ 层级)。

    Returns
    -------
    str
        存档号。找不到或单实例时返回 "0"。
    """
    # ---- 第一优先: 环境变量 (预留给上游 MFAAvalonia 注入) ----
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    if instance_id:
        logger.info(f"[Resolver] ✅ 从环境变量获取 instance_id = {instance_id}")
    else:
        # ---- 第二优先: 日志反查 ----
        instance_id = _find_instance_from_log(socket_id, project_root)
        if instance_id:
            logger.info(f"[Resolver] ✅ 从日志反查获取 instance_id = {instance_id}")
        else:
            logger.info("[Resolver] 未检测到多实例上下文，使用默认存档")
            return "0"

    # ---- 单实例判定: "default" 实例不需要额外存档号 ----
    if instance_id == "default":
        logger.info("[Resolver] 当前为默认实例 (default)，使用存档 0")
        return "0"

    # ---- 从实例配置文件中提取用户自定义存档号 ----
    account_id = _extract_account_from_config(instance_id, project_root)
    logger.info(f"[Resolver] 📋 最终存档号 = {account_id} (instance={instance_id})")
    return account_id


# =============================================================================
# 日志反查
# =============================================================================

# 匹配日志中的实例标签和 Agent 标识符
# 示例行: [inst=配置 2/5f398f16][src=Worker]... Agent 标识符：SIWlSREj
_RE_AGENT_ID = re.compile(
    r"\[inst=[^/]+/([^\]]+)\]"   # 捕获组1: instance_id (斜杠后到]之间)
    r".*"
    r"Agent 标识符[：:]"          # 兼容全角/半角冒号
    r"\s*(\S+)"                   # 捕获组2: socket_id
)


def _find_instance_from_log(socket_id: str, project_root: Path) -> str | None:
    """
    从 MFA 日志中根据 socket_id 反查 instance_id。

    策略:
      - 定位最新的日志文件
      - 从文件末尾向前搜索 (最近的启动记录在尾部)
      - 用 socket_id 做精确匹配，同行提取 instance_id
    """
    log_dir = project_root / "logs"
    if not log_dir.is_dir():
        logger.warning(f"[Resolver] 日志目录不存在: {log_dir}")
        return None

    # 找到今天(或最近的)日志文件
    log_file = _find_latest_log(log_dir)
    if not log_file:
        logger.warning("[Resolver] 未找到可用的日志文件")
        return None

    logger.info(f"[Resolver] 正在搜索日志: {log_file.name} (关键词: {socket_id})")

    try:
        # 读取全部行，从后往前搜索
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in reversed(lines):
            # 快速预筛: 行中必须同时包含 socket_id 和 "Agent" 关键字
            if socket_id not in line:
                continue
            if "Agent" not in line:
                continue

            m = _RE_AGENT_ID.search(line)
            if m and m.group(2) == socket_id:
                return m.group(1)  # instance_id

    except Exception as e:
        logger.warning(f"[Resolver] 日志读取异常: {e}")

    logger.warning(f"[Resolver] 在日志中未找到 socket_id={socket_id} 对应的实例记录")
    return None


def _find_latest_log(log_dir: Path) -> Path | None:
    """
    在日志目录中定位最新的日志文件。

    优先匹配今天的 log-YYYYMMDD.log，
    找不到则按修改时间取最新的 .log 文件。
    """
    # 尝试今天的日志
    today_str = datetime.now().strftime("%Y%m%d")
    today_log = log_dir / f"log-{today_str}.log"
    if today_log.is_file():
        return today_log

    # 降级: 按修改时间取最新
    log_files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return log_files[0] if log_files else None


# =============================================================================
# 配置文件解析
# =============================================================================

def _extract_account_from_config(instance_id: str, project_root: Path) -> str:
    """
    从 config/instances/{instance_id}.json 中提取用户自定义的存档号。

    搜索逻辑:
      遍历 TaskItems → option → 找到 name 包含 "多存档" 的选项
      → 进入其 sub_options → 找到 data 中的 "账号多开配置" 字段

    找不到时返回 "0"。
    """
    config_path = project_root / "config" / "instances" / f"{instance_id}.json"
    if not config_path.is_file():
        logger.warning(f"[Resolver] 实例配置不存在: {config_path}")
        return "0"

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        logger.warning(f"[Resolver] 读取实例配置失败: {e}")
        return "0"

    # 遍历所有 TaskItem 的 option 树
    for task in config.get("TaskItems", []):
        for option in task.get("option", []):
            if "多存档" not in option.get("name", ""):
                continue

            # 找到「多存档」选项，向下搜索 sub_options
            for sub in option.get("sub_options", []):
                data = sub.get("data", {})
                account = data.get("账号多开配置", "").strip()
                if account:
                    return account

            # 「多存档」选项存在但用户未填写存档号
            logger.info("[Resolver] 检测到「多存档」选项但未配置存档名称")
            return "0"

    # 该实例配置中没有「多存档」选项 (用户可能未勾选此任务)
    logger.info("[Resolver] 实例配置中未找到「多存档」选项，使用默认存档")
    return "0"
