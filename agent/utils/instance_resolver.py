# -*- coding: utf-8 -*-
"""
实例探测器 (Instance Resolver)
==============================
解析当前 Agent 进程归属的 MFA 实例，**仅供日志排查多实例问题**。

探测链路 (按优先级):
  1. 环境变量 MFA_INSTANCE_ID  → MFAAvalonia v2.11.3+ 注入 (首选)
     附带 MFA_INSTANCE_NAME    → 实例显示名称 (如 "配置 2")
  2. 日志反查 socket_id        → 从 MFA 日志提取 [inst=.../instance_id] (旧版降级)

本模块不再解析存档号
--------------------
原先这里会去读 config/instances/{id}.json 里用户填的存档号。该实现遍历的是
`TaskItems`，但「存档名称」挂在 global_option 下，MFAAvalonia 把它写进的是**另一个
平级键 `GlobalOptionItems`** —— 于是那段代码永远找不到，恒返回 "0"，并附带一条
误报的"防串档"警告。

修它意味着在 py 侧重新实现一遍 MFAAvalonia 的 option 解析（index / sub_options /
data 三层），而那套结构会随上游演进漂移，迟早再坏一次。正确的权威是运行时的
`context.get_node_data`——它拿到的是经过完整 option 合并管线之后的值。

所以存档号统一由 utils/account_sync.py 在 custom 回调里从 context 读取，
本模块只留实例身份探测。多实例串档的风险也随之消失：每个实例的 context 里
带的本就是它自己的存档号，不存在"回退到公共档"这回事。
"""

import os
import re
from pathlib import Path
from datetime import datetime

from . import mfaalog as logger

# =============================================================================
# 公开接口
# =============================================================================

def resolve_instance_id(socket_id: str, project_root: Path) -> str | None:
    """
    解析当前进程归属的实例 ID，并记入日志。**不涉及存档号。**

    Parameters
    ----------
    socket_id : str
        AgentServer 握手标识符，即 sys.argv 传入的 socket_id。
    project_root : Path
        项目根目录 (install/ 层级)。

    Returns
    -------
    str | None
        实例 ID；未检测到多实例上下文时返回 None。
    """
    # ---- 第一优先: 环境变量 (MFAAvalonia v2.11.3+ 注入) ----
    instance_id = os.environ.get("MFA_INSTANCE_ID", "").strip()
    if instance_id:
        instance_name = os.environ.get("MFA_INSTANCE_NAME", "")
        logger.info(f"[Resolver] ✅ 从环境变量获取 instance_id = {instance_id}"
                    + (f" ({instance_name})" if instance_name else ""))
        return instance_id

    # ---- 第二优先: 日志反查 (兼容未注入环境变量的旧版 MFAAvalonia) ----
    instance_id = _find_instance_from_log(socket_id, project_root)
    if instance_id:
        logger.info(f"[Resolver] ✅ 从日志反查获取 instance_id = {instance_id}")
        return instance_id

    logger.info("[Resolver] 未检测到多实例上下文")
    return None


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
