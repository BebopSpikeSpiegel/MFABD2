"""timer.py（墙钟计时闸）的离线单测：不连模拟器、不起 AgentServer，只驱动纯逻辑。

跑法（从仓库根执行）：
    cd agent && python -m unittest recognition.test_timer

⚠️ 与同目录的 test_rdd_hsv_rescue.py 不同，本文件**只有这一条跑法**。
那个模块是零依赖的纯算法，怎么跑都行；timer.py 则 `from utils import mfaalog`，
而 utils 在 `agent/` 下。直接 `python agent/recognition/test_timer.py` 或
`unittest discover -s agent/recognition` 会把 sys.path[0] 落在 `agent/recognition/`，
解析不到 utils，报 ModuleNotFoundError。本项目的包根是 `agent/`，上面那条跑法落在
`recognition` 这个正确的包根上——理由与 test_rdd_hsv_rescue.py 文件头说的是同一条。

这里钉住的多数是**反直觉**的决策，它们看着像 bug，其实每条都是评审后定下的：
守卫故障一律放行、参数漏配按不限时而不是按 30 分钟、未超时时一行日志都不打。
改动 timer.py 前先读这些用例，别顺手"修"回去。
"""

import time
import unittest

try:
    from . import timer
except ImportError:
    import timer


class _Argv:
    """冒充 AnalyzeArg / RunArg：两个类各自只读自己那个 param 字段。"""

    def __init__(self, raw):
        self.custom_recognition_param = raw
        self.custom_action_param = raw


class _LogCatcher:
    """替掉 timer 里的 mfaalog，把每条日志记下来。"""

    def __init__(self):
        self.records = []

    def _make(self, level):
        return lambda msg: self.records.append((level, str(msg)))

    def __getattr__(self, level):
        return self._make(level)

    def levels(self):
        return [lvl for lvl, _ in self.records]


class TimerTestCase(unittest.TestCase):
    def setUp(self):
        timer.TIMER_STORE.clear()
        self.log = _LogCatcher()
        self._real_logger = timer.logger
        timer.logger = self.log

    def tearDown(self):
        timer.logger = self._real_logger
        timer.TIMER_STORE.clear()

    # ---- 工具 ----

    def reco(self, raw):
        return timer.TimerExpired().analyze(None, _Argv(raw))

    def act(self, cls, raw):
        return cls().run(None, _Argv(raw))

    def start_ago(self, name, seconds):
        """把某个计时器的起点挪到 seconds 秒之前。"""
        timer.TIMER_STORE[name] = time.monotonic() - seconds


class TestUnlimited(TimerTestCase):
    """0 / 省略 / 坏值都走「不限时」，且这一支不起表、不打日志。"""

    def test_missing_minutes_is_unlimited(self):
        # py 侧默认必须是不限时：上限该由 pipeline 节点给，漏配时放行而不是自己定一个数
        self.assertIsNone(self.reco('{"timer": "T"}'))
        self.assertEqual(timer.TIMER_STORE, {})
        self.assertEqual(self.log.records, [])

    def test_zero_minutes_is_unlimited(self):
        self.assertIsNone(self.reco('{"timer": "T", "minutes": 0}'))
        self.assertEqual(timer.TIMER_STORE, {})

    def test_negative_minutes_is_unlimited(self):
        self.assertIsNone(self.reco('{"timer": "T", "minutes": -5}'))

    def test_unparsable_minutes_falls_back_to_unlimited(self):
        # 不是回落到 30——那会变成「参数坏了就强制 30 分钟收工」，与放行策略反向
        self.assertIsNone(self.reco('{"timer": "T", "minutes": "abc"}'))
        self.assertEqual(timer.TIMER_STORE, {})
        self.assertIn("error", self.log.levels())

    def test_unlimited_path_is_silent(self):
        # 不限时恰恰是挂机最久、被问得最多的场景，一行日志都不能打
        for _ in range(50):
            self.reco('{"timer": "T", "minutes": 0}')
        self.assertEqual(self.log.records, [])


class TestNotExpired(TimerTestCase):
    def test_pending_returns_box_none_with_detail(self):
        # box=None 判未命中，但 detail 仍会写进识别记录 → 状态走 maafw.log 而不是刷 GUI
        self.start_ago("T", 60)
        result = self.reco('{"timer": "T", "minutes": 30}')
        self.assertIsNotNone(result)
        self.assertIsNone(result.box)
        self.assertEqual(result.detail["limit_sec"], 1800.0)
        self.assertGreaterEqual(result.detail["elapsed_sec"], 60.0)

    def test_pending_logs_nothing(self):
        # 这个闸门挂在回环节点 next 首位，每一拍轮询都会问一次
        self.start_ago("T", 60)
        for _ in range(50):
            self.reco('{"timer": "T", "minutes": 30}')
        self.assertEqual(self.log.records, [])

    def test_lazy_start_when_absent(self):
        # 惰性起表是防 KeyError 的兜底：key 不存在时就地起表并判未超时
        result = self.reco('{"timer": "T", "minutes": 30}')
        self.assertIsNone(result.box)
        self.assertIn("T", timer.TIMER_STORE)


class TestExpired(TimerTestCase):
    def test_expired_hits_and_clears_store(self):
        # 命中即停：使命完成就把起点清掉，别留给下一次运行
        self.start_ago("T", 1801)
        result = self.reco('{"timer": "T", "minutes": 30}')
        self.assertEqual(result.box, [0, 0, 0, 0])
        self.assertNotIn("T", timer.TIMER_STORE)
        self.assertIn("info", self.log.levels())

    def test_expired_detail_carries_elapsed(self):
        self.start_ago("T", 1801)
        result = self.reco('{"timer": "T", "minutes": 30}')
        self.assertGreaterEqual(result.detail["elapsed_sec"], 1801.0)
        self.assertEqual(result.detail["limit_sec"], 1800.0)

    def test_stale_start_point_expires_immediately(self):
        # 已知边界：残留旧起点会一进去就判超时。惰性起表挡不住这个——
        # 那种情况 key 是存在的、值是上一轮的，走不到惰性起表那一支。
        # 所以入口必须挂 StartTimer，别指望兜底。
        self.start_ago("T", 99999)
        self.assertEqual(self.reco('{"timer": "T", "minutes": 30}').box, [0, 0, 0, 0])

    def test_fractional_minutes(self):
        self.start_ago("T", 31)
        self.assertEqual(self.reco('{"timer": "T", "minutes": 0.5}').box, [0, 0, 0, 0])


class TestStartTimer(TimerTestCase):
    def test_start_creates_timer(self):
        self.assertIs(self.act(timer.StartTimer, '{"timer": "T"}'), True)
        self.assertIn("T", timer.TIMER_STORE)

    def test_reset_true_by_default(self):
        timer.TIMER_STORE["T"] = 111.0
        self.act(timer.StartTimer, '{"timer": "T"}')
        self.assertNotEqual(timer.TIMER_STORE["T"], 111.0)

    def test_reset_false_keeps_origin(self):
        timer.TIMER_STORE["T"] = 111.0
        self.act(timer.StartTimer, '{"timer": "T", "reset": false}')
        self.assertEqual(timer.TIMER_STORE["T"], 111.0)

    def test_broken_param_still_returns_true(self):
        # 返回 False 会让节点进错误态走 on_error，而本仓库按约定不写 on_error，
        # 结果是整条链断在入口、爬塔根本起不来。守卫自己坏掉不该拖累主流程。
        self.assertIs(self.act(timer.StartTimer, "{ 这不是 json"), True)
        self.assertIn("error", self.log.levels())


class TestResetTimer(TimerTestCase):
    def test_reset_single_name(self):
        timer.TIMER_STORE.update({"A": 1.0, "B": 2.0})
        self.assertIs(self.act(timer.ResetTimer, '{"timer": "A"}'), True)
        self.assertEqual(timer.TIMER_STORE, {"B": 2.0})

    def test_reset_list_ignores_missing(self):
        timer.TIMER_STORE.update({"A": 1.0, "C": 3.0})
        self.act(timer.ResetTimer, '{"timer": ["A", "B", "nope"]}')
        self.assertEqual(timer.TIMER_STORE, {"C": 3.0})

    def test_reset_without_target_is_noop(self):
        timer.TIMER_STORE.update({"A": 1.0})
        self.assertIs(self.act(timer.ResetTimer, "{}"), True)
        self.assertEqual(timer.TIMER_STORE, {"A": 1.0})

    def test_broken_param_still_returns_true(self):
        self.assertIs(self.act(timer.ResetTimer, "{ 坏 json"), True)
        self.assertIn("error", self.log.levels())


class TestTimerGate(TimerTestCase):
    """门禁形态：未超时 True(走 next)，超时清零 + False(走 on_error)。

    语义固定、不提供反转参数——反着配会让每一轮都进错误处理路径，而 error_handling
    为真时引擎不弹回跳栈。别为了省掉写 on_error 的负担把它加回来。
    """

    def gate(self, raw):
        return self.act(timer.TimerGate, raw)

    def test_absent_tag_starts_and_passes(self):
        # Gate 兼做入口：无 tag 就地起表并放行，不需要另挂 StartTimer
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), True)
        self.assertIn("T", timer.TIMER_STORE)

    def test_pending_passes(self):
        self.start_ago("T", 60)
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), True)
        self.assertIn("T", timer.TIMER_STORE)

    def test_expired_blocks_and_clears(self):
        self.start_ago("T", 1801)
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), False)
        self.assertNotIn("T", timer.TIMER_STORE)
        self.assertIn("info", self.log.levels())

    def test_expired_then_reentry_restarts(self):
        # 闭环：超时那一刻清零 → 下次进来是无 tag → 惰性起表 → 重新放行。
        # 这是「不必额外挂 ResetTimer」的全部依据，改动清零逻辑前先看这条。
        self.start_ago("T", 1801)
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), False)
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), True)
        self.assertIn("T", timer.TIMER_STORE)

    def test_unlimited_always_passes(self):
        self.start_ago("T", 99999)
        self.assertIs(self.gate('{"timer": "T", "minutes": 0}'), True)
        self.assertIs(self.gate('{"timer": "T"}'), True)

    def test_unlimited_does_not_start_timer(self):
        self.gate('{"timer": "T", "minutes": 0}')
        self.assertEqual(timer.TIMER_STORE, {})
        self.assertEqual(self.log.records, [])

    def test_pending_logs_nothing(self):
        # Gate 每轮循环执行一次，循环体短的话照样能刷屏
        self.start_ago("T", 60)
        for _ in range(50):
            self.gate('{"timer": "T", "minutes": 30}')
        self.assertEqual(self.log.records, [])

    def test_unparsable_minutes_passes(self):
        self.start_ago("T", 99999)
        self.assertIs(self.gate('{"timer": "T", "minutes": "abc"}'), True)
        self.assertIn("error", self.log.levels())

    def test_broken_param_still_passes(self):
        # 守卫自己坏掉不该拦住主流程 —— 与 StartTimer / ResetTimer 同向
        self.assertIs(self.gate("{ 这不是 json"), True)
        self.assertIn("error", self.log.levels())

    def test_shares_pool_with_recognition_form(self):
        # 文档承诺两个形态共用同一个计时器池子，这里钉住
        self.act(timer.StartTimer, '{"timer": "T"}')
        self.assertIs(self.gate('{"timer": "T", "minutes": 30}'), True)
        self.act(timer.ResetTimer, '{"timer": "T"}')
        self.assertEqual(timer.TIMER_STORE, {})


class TestFailOpen(TimerTestCase):
    """闸门自身出问题时一律不拦截：识别侧 None，动作侧 True。"""

    def test_recognition_fails_open_on_broken_param(self):
        self.assertIsNone(self.reco("{ 坏 json"))
        self.assertIn("error", self.log.levels())

    def test_recognition_fails_open_on_internal_error(self):
        class Boom:
            @property
            def custom_recognition_param(self):
                raise RuntimeError("boom")

        self.assertIsNone(timer.TimerExpired().analyze(None, Boom()))
        self.assertIn("error", self.log.levels())


# 不写 `if __name__ == "__main__": unittest.main()`：直接运行本文件时，
# import 阶段就会因为解析不到 utils 而中止，那段代码永远执行不到。跑法见文件头。
