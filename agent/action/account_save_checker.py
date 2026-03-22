import json
import utils
from utils.persistent_store import PersistentStore

from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer

# ==============================================================================
# 🔄 账号切换检查点 (Account Switch Checkpoint) — 降级为兜底验证
# ==============================================================================
# 多存档隔离现已在 Agent 启动时由 instance_resolver 一次性完成。
# 本 Action 保留为兜底: 若 Pipeline 通过 custom_action_param 显式传入
# account_id，仍会触发切换。跨 Task 参数丢失时，依靠进程级常驻的
# _current_account_id 继续运行，无需任何操作。
# ==============================================================================

@AgentServer.custom_action("SwitchAccountCheckpoint")
class SwitchAccountCheckpointAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """
        兜底账号切换。始终返回 True，不阻断 Pipeline。
        """
        try:
            # 解析参数
            if hasattr(argv, 'custom_action_param'):
                param_str = getattr(argv, 'custom_action_param', '{}')
                params = json.loads(param_str) if isinstance(param_str, str) else param_str
            elif isinstance(argv, dict):
                params = argv
            else:
                params = {}
            
            # 仅在显式传入 account_id 时才触发切换
            if "account_id" in params:
                account_id = params["account_id"]
                PersistentStore.switch_account(account_id)
            # 否则静默通过 — instance_resolver 已在启动时完成挂载
            
            return True
            
        except Exception as e:
            utils.mfaalog.error(f"[Py] ❌ 账号切换检查点执行异常: {e}")
            return True
