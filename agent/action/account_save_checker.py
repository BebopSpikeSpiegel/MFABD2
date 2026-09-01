import utils
from utils.account_sync import sync_from_context

from maa.custom_action import CustomAction
from maa.context import Context
from maa.agent.agent_server import AgentServer

# ==============================================================================
# 🔄 账号切换检查点 (Account Switch Checkpoint)
# ==============================================================================
# 本 Action 是存档号同步的 **push 路径**：挂在 StartGame 链首
# (`StartGame_Start` 的 next → [JumpBack]Env_AccountSave_Switch)，提供一次
# 早期同步与用户可见的 focus 回调。
#
# 但它不是唯一路径，也不该是 —— 用户常常不从「启动脚本」这个 task 起跑，
# 此时本节点根本不会被执行。真正保证正确性的是 **pull 路径**：
# cartridge_lib 的每个 custom 入口在读写存档前自行同步一次
# (见 utils/account_sync.py 的模块 docstring)。
#
# 两条路调用同一个 sync_from_context，按值比较、互相幂等，
# 谁先跑到都一样，重复跑也无副作用。
# ==============================================================================

@AgentServer.custom_action("SwitchAccountCheckpoint")
class SwitchAccountCheckpointAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        """同步存档号。始终返回 True，不阻断 Pipeline。

        从 context 读而不是从 argv 读：pull 路径拿不到本节点的 argv，
        两边都走 context 才是同一份实现、同一个取值路径，不会出现
        「两条路读到不同值」。本节点是 custom_action_param 的唯一载体，
        所以 argv 与 context 读到的本就是同一份数据。

        恒返回 True 的理由：存档同步失败不该让整条业务链作废。失败时
        sync_from_context 已发出告警且保持存档号不变，**不会因为回落默认档
        而写错**；但这不等于"一定写对" —— 若此前曾成功切到过别的档，保持
        不变同样可能是错的档。为什么仍然选择放行而不是拦截，见
        utils/account_sync.py 的「失败时为什么不拦截业务」一节。
        """
        try:
            sync_from_context(context, where="SwitchAccountCheckpoint")
        except Exception as e:
            utils.mfaalog.error(f"[Py] ❌ 账号切换检查点执行异常: {e}")
        return True
