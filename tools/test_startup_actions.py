"""Offline checks for controller-specific StartGame action dispatch."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
from startup.common import Prepared


class StartupActionTests(unittest.TestCase):
    def setUp(self):
        self.guard = types.SimpleNamespace(ensure=Mock(return_value=Prepared()))
        modules = {}
        for name in ("maa", "maa.agent", "maa.agent.agent_server", "maa.custom_action", "startup.sink", "utils"):
            modules[name] = types.ModuleType(name)
        modules["maa.agent.agent_server"].AgentServer = types.SimpleNamespace(custom_action=lambda name: lambda cls: cls)
        modules["maa.custom_action"].CustomAction = object
        modules["startup.sink"].guard = self.guard
        modules["utils"].mfaalog = types.SimpleNamespace(info=Mock(), error=Mock())
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("test_startup_adapter", ROOT / "agent/action/startup_prepare.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)

    def context(self, kind):
        controller = types.SimpleNamespace(info={"type": kind}, post_start_app=Mock())
        return types.SimpleNamespace(
            tasker=types.SimpleNamespace(controller=controller, stopping=False, post_stop=Mock()),
            run_action=Mock(return_value=types.SimpleNamespace(success=True)),
        )

    def test_playcover_continues_manual_game_without_start_app(self):
        context = self.context("playcover")
        self.assertTrue(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_not_called()
        context.tasker.post_stop.assert_not_called()

    def test_prepared_pc_does_not_launch_again(self):
        context = self.context("win32")
        self.assertTrue(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_not_called()
        context.run_action.assert_not_called()

    def test_adb_check_uses_legacy_action_without_startup_guard(self):
        context = self.context("adb")
        for result, expected in ((types.SimpleNamespace(success=True), True),
                                 (types.SimpleNamespace(success=False), False), (None, False)):
            context.run_action.return_value = result
            self.assertEqual(self.module.StartupCheckApp().run(context, None), expected)
        self.assertEqual(context.run_action.call_args.args, ("StartGame_ADB_Check_App_Alive",))
        self.guard.ensure.assert_not_called()
        context.tasker.post_stop.assert_not_called()

    def test_adb_launch_and_fallback_use_separate_legacy_actions(self):
        context = self.context("adb")
        for entry, target in (("StartGame_RunApp", "StartGame_ADB_RunApp"),
                              ("StartGame_RunApp_Shell", "StartGame_ADB_RunApp_Shell")):
            for result, expected in ((types.SimpleNamespace(success=True), True),
                                     (types.SimpleNamespace(success=False), False), (None, False)):
                context.run_action.return_value = result
                self.assertEqual(self.module.StartupRunApp().run(
                    context, types.SimpleNamespace(node_name=entry)), expected)
                self.assertEqual(context.run_action.call_args.args, (target,))
        self.guard.ensure.assert_not_called()
        context.tasker.post_stop.assert_not_called()

    def test_non_adb_checks_do_not_run_legacy_shell(self):
        for kind in ("win32", "playcover", "native_android", "custom"):
            context = self.context(kind)
            self.assertFalse(self.module.StartupCheckApp().run(context, None))
            context.run_action.assert_not_called()

    def test_other_controller_keeps_its_start_app_path(self):
        context = self.context("custom")
        context.tasker.controller.post_start_app.return_value = types.SimpleNamespace(done=True, succeeded=True)
        self.assertTrue(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_called_once_with("com.neowizgames.game.browndust2")

    def test_failed_guard_is_not_bypassed(self):
        self.guard.ensure.return_value = None
        context = self.context("playcover")
        self.assertFalse(self.module.StartupRunApp().run(context, None))
        context.tasker.controller.post_start_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
