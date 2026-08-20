from maa.custom_recognition import CustomRecognition
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer
import json
import time

from utils import mfaalog as logger

# ⏱️ 墙钟计时闸 (Wall-clock Timer Gate)
# ==============================================================================
# 与 counter.py 的关系: counter 管「做了多少次」, 本模块管「跑了多久」。
# 两者互补——有些流程的轮数天生不确定(打到输为止、爬到爬不动为止), 用次数无法表达
# 「我最多愿意让它跑多久」, 只能用墙钟。
#
# 为什么需要它(实测背景):
#   半自动爬塔(SemiAuto_Evil_SemiautoTower_*)在 pipeline 里**没有任何终止条件**:
#   收尾节点 SemiAuto_Evil_SemiautoTower_End 全仓无人 next 到它, 整个环只能靠战败结束。
#   一旦战斗判定卡住(或玩家中途接管把流程弹回手动分支), 它可以一直跑下去。
#   这类「轮数由玩家意愿决定」的流程, 正确的界不是次数而是时间。
#
# 设计要点:
#   · 用 time.monotonic() 而非 time.time(): 只测间隔, 不受系统时间调整/时区影响。
#   · 惰性启动: TimerExpired 首次被问到时若计时器不存在, 就地起表并判为「未超时」。
#     这样即使某个入口忘了挂 StartTimer, 保护依然生效(晚起表只会让上限偏宽, 不会误杀)。
#   · 计时器存在内存里, 进程重启即清零——这正是期望行为: 每次运行重新计时。
#
# ------------------------------------------------------------------------------
# 📝 Pipeline 配置指南
# ------------------------------------------------------------------------------
#
# 【功能 A】TimerExpired - 超时闸 (作为 "recognition" 使用)
# ---------------------------------------------------
# 逻辑(注意是反的): 已超时 -> 识别成功 -> 走 next 去收尾节点
#                   未超时 -> 识别失败 -> 引擎自动尝试 next 列表里的下一个候选, 流程照常
# 因此把它挂在循环节点 next 的**首位**即可, 不需要改动原有候选的顺序语义。
#
# "SemiAuto_Evil_SemiautoTower_Timeout": {
#     "recognition": "Custom",
#     "custom_recognition": "TimerExpired",
#     "custom_recognition_param": {
#         "timer": "EvilTower",        // 计时器名, 与 StartTimer 一致
#         "minutes": 30                 // 上限, 支持小数; 0 或负数 = 不限时(闸门永不触发)
#     },
#     "next": ["SemiAuto_Evil_SemiautoTower_End"]
# }
#
# 【功能 B】StartTimer - 起表 (作为 "action" 使用, 可选)
# ---------------------------------------------------
# 放在流程入口, 让计时起点精确到「开始爬塔」而不是「第一次问闸门」。
# 重复调用默认会重新起表(reset=true), 传 reset:false 则只在不存在时起表。
#
# "SemiAuto_Evil_SemiautoTower_Start": {
#     "action": "Custom",
#     "custom_action": "StartTimer",
#     "custom_action_param": {"timer": "EvilTower"},
#     "next": ["SemiAuto_Evil_SemiautoTower_On"]
# }
# ==============================================================================

# 计时器仓库: {名字: 起点(monotonic 秒)}
TIMER_STORE = {}


def _elapsed(name):
    """返回已用秒数; 计时器不存在则惰性起表并返回 0。"""
    if name not in TIMER_STORE:
        TIMER_STORE[name] = time.monotonic()
        logger.debug(f"⏱️ [计时器惰性启动] {name}")
        return 0.0
    return time.monotonic() - TIMER_STORE[name]


def _fmt(sec):
    m, s = divmod(int(sec), 60)
    return f"{m}分{s:02d}秒"


# =========================================================
# 1. 识别：超时闸 (已超时才算识别成功)
# 参数: { "timer": "EvilTower", "minutes": 30 }
# =========================================================
@AgentServer.custom_recognition("TimerExpired")
class TimerExpired(CustomRecognition):
    def analyze(self, context, argv):
        try:
            params = json.loads(argv.custom_recognition_param)
            name = params.get("timer", "default")
            # interface.json 的 input 注入过来可能是字符串, 统一转数
            try:
                minutes = float(params.get("minutes", 30))
            except (TypeError, ValueError):
                logger.error(f"TimerExpired: minutes 参数无法解析({params.get('minutes')!r}), 按 30 分钟处理")
                minutes = 30.0

            elapsed = _elapsed(name)

            # 0 或负数 = 不限时。给玩家一个「我不想要这个保护」的出口。
            if minutes <= 0:
                logger.debug(f"⏱️ [{name}] 上限设为 {minutes}, 视为不限时, 闸门不触发")
                return None

            limit = minutes * 60.0
            if elapsed >= limit:
                logger.info(f"🛑 [{name}] 已达时间上限 {minutes} 分钟(实际 {_fmt(elapsed)}) → 收工")
                return CustomRecognition.AnalyzeResult(
                    box=[0, 0, 0, 0],
                    detail={
                        "msg": f"{name} 超时 {_fmt(elapsed)}/{minutes}分",
                        "elapsed_sec": round(elapsed, 1),
                        "limit_sec": limit,
                    },
                )

            logger.debug(f"⏱️ [{name}] {_fmt(elapsed)} / {minutes}分, 继续")
            return None
        except Exception as e:
            # 闸门自身异常时选择「不拦截」: 宁可让流程照常跑, 也不要因为守卫故障而中断正常任务
            logger.error(f"TimerExpired 异常: {e}")
            return None


# =========================================================
# 2. 动作：起表
# 参数: { "timer": "EvilTower", "reset": true }
# =========================================================
@AgentServer.custom_action("StartTimer")
class StartTimer(CustomAction):
    def run(self, context, argv):
        try:
            params = json.loads(argv.custom_action_param)
            name = params.get("timer", "default")
            reset = params.get("reset", True)

            if reset or name not in TIMER_STORE:
                TIMER_STORE[name] = time.monotonic()
                logger.debug(f"⏱️ [计时器起表] {name}")
            else:
                logger.debug(f"⏱️ [计时器已存在, 保持原起点] {name} 已跑 {_fmt(_elapsed(name))}")
            return True
        except Exception as e:
            logger.error(f"StartTimer 异常: {e}")
            return False
