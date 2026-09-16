"""Compatibility adapters for existing StartGame pipeline entry names."""

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from startup.common import Budget
from startup.sink import guard
from utils import mfaalog


@AgentServer.custom_action("StartupCheckApp")
class StartupCheckApp(CustomAction):
    def run(self, context, argv):
        if context.tasker.controller.info.get("type") == "adb":
            result = context.run_action("StartGame_ADB_Check_App_Alive")
            return result is not None and result.success
        result = guard.ensure(context)
        if result is None:
            return False
        kind = context.tasker.controller.info.get("type")
        # 非 ADB 平台继续进入原有加载分支；ADB 已在上方执行旧版探测。
        # PC may have launched in the separate pretask process, so enter the
        # loading branch (which also recognizes an already-ready home screen).
        return kind == "adb" and not result.changed


@AgentServer.custom_action("StartupRunApp")
class StartupRunApp(CustomAction):
    def run(self, context, argv):
        if context.tasker.controller.info.get("type") == "adb":
            node = ("StartGame_ADB_RunApp_Shell" if argv.node_name == "StartGame_RunApp_Shell"
                    else "StartGame_ADB_RunApp")
            result = context.run_action(node)
            return result is not None and result.success
        result = guard.ensure(context)
        if result is None:
            return False
        controller = context.tasker.controller
        if controller.info.get("type") in ("adb", "win32", "playcover"):
            return True
        # PlayCover must already be running to connect; StartApp is unsupported.
        # Native Android overrides this node with StartApp in its resource layer.
        try:
            budget = Budget(mfaalog.info, lambda: context.tasker.stopping)
            job = controller.post_start_app("com.neowizgames.game.browndust2")
            while not job.done:
                budget.pause(0.1)
            return job.succeeded
        except Exception as exc:
            mfaalog.error(f"[启动准备] 控制器启动游戏失败：{exc}")
            context.tasker.post_stop()
            return False
