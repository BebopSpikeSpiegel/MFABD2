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
#   半自动爬塔(SemiAuto_Evil_SemiautoTower_*)一层接一层地往上爬, pipeline 里没有任何
#   终止条件。注意真卡死并不是问题所在——所有 next 候选都识别不中时, 节点会重试到
#   timeout, 进错误态后由默认的 on_error(Global_Null) 静默退栈, 框架自己兜得住。
#   真正闸不住的是「环在转但不前进」: 比如 InAuto 与 AuDaemon 来回弹, 每一拍都有候选
#   命中, 框架的 timeout 永远不触发。再加上「玩家本就不打算停」这个正常场景——
#   这类流程的界只能是时间, 不是次数。
#
# 设计要点:
#   · 用 time.monotonic() 而非 time.time(): 只测间隔, 不受系统时间调整/时区影响。
#   · 计时器存在内存里, 进程重启即清零——这正是期望行为: 每次运行重新计时。
#   · 闸门自身出问题时一律「不拦截」: 识别侧返回 None, 动作侧返回 True。
#     守卫故障不该拖累主流程, 宁可让任务照常跑完。
#   · 未超时不打日志, 改把状态塞进 AnalyzeResult.detail(box=None 仍判未命中)。
#     闸门挂在回环节点 next 首位, 引擎每一拍轮询都会问一次 analyze, 频率由引擎节奏
#     决定、代码这边控制不了; 而 mfaalog 没有级别开关, debug() 走到就一定打给 GUI。
#     状态走 maafw.log 的识别记录, 不刷 GUI。
#
# ⚠️ 生命周期必须在 pipeline 里交代完整:
#   TIMER_STORE 是模块级全局, agent 进程跨多次运行是活着的。所以入口挂 StartTimer 起表、
#   超时命中时自动清表、中途放弃用 ResetTimer 作废。_elapsed 的惰性起表只是防 KeyError
#   的兜底, **挡不住残留的旧起点**——那种情况 key 是存在的、值是上一轮的, 一进去就直接
#   判超时。别把它当成「忘挂 StartTimer 也没事」的保证。
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
#         "minutes": 30                 // 上限, 支持小数; 省略/0/负数 = 不限时(闸门永不触发)
#     },
#     "next": ["SemiAuto_Evil_SemiautoTower_End"]
# }
#
# 【功能 B】StartTimer - 起表 (作为 "action" 使用)
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
#
# 【功能 C】ResetTimer - 作废计时器 (作为 "action" 使用)
# ---------------------------------------------------
# 中途放弃、换一轮重来这类场景用。支持单个名字或名字列表。
# 超时命中时闸门会自己清表, 那条路径不需要再显式 Reset。
#
# "Xxx_Abort": {
#     "action": "Custom",
#     "custom_action": "ResetTimer",
#     "custom_action_param": {"timer": ["EvilTower"]}
# }
# ==============================================================================

# 计时器仓库: {名字: 起点(monotonic 秒)}
TIMER_STORE = {}


def _elapsed(name):
    """返回已用秒数; 计时器不存在则就地起表并返回 0。

    这是防 KeyError 的兜底, 不是「忘挂 StartTimer 也没事」的保证——
    残留旧起点的场景里 key 是存在的, 根本走不到这一支。
    """
    if name not in TIMER_STORE:
        TIMER_STORE[name] = time.monotonic()
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
            # 默认不限时: 上限该由 pipeline 节点给, py 侧漏配时放行而不是自作主张定一个数
            try:
                minutes = float(params.get("minutes", 0))
            except (TypeError, ValueError):
                logger.error(f"TimerExpired: minutes 参数无法解析({params.get('minutes')!r}), 按不限时处理")
                minutes = 0.0

            # 0 或负数 = 不限时。放在最前面直接返回: 不起表, 一行日志不打——
            # 不限时恰恰是挂机最久、被问得最多的场景。
            if minutes <= 0:
                return None

            name = params.get("timer", "default")
            elapsed = _elapsed(name)
            limit = minutes * 60.0
            status = {
                "msg": f"{name} {_fmt(elapsed)}/{minutes}分",
                "elapsed_sec": round(elapsed, 1),
                "limit_sec": limit,
            }

            if elapsed >= limit:
                # 使命已完成, 顺手清表, 别把起点留给下一次运行
                TIMER_STORE.pop(name, None)
                logger.info(f"🛑 [{name}] 已达时间上限 {minutes} 分钟(实际 {_fmt(elapsed)}) → 收工")
                return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail=status)

            # box=None 判定为未命中, 但 detail 仍会写进识别记录(见 maa/custom_recognition.py:
            # detail_buffer.set 在 if 之外无条件执行)。状态因此走 maafw.log 而不刷 GUI。
            return CustomRecognition.AnalyzeResult(box=None, detail=status)
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
        except Exception as e:
            # 记 error 但仍返回 True: 返回 False 会让本节点进错误态走 on_error,
            # 而本仓库按约定不写 on_error, 结果是整条链断在入口、爬塔根本起不来。
            logger.error(f"StartTimer 异常: {e}")
        return True


# =========================================================
# 3. 动作：作废计时器
# 参数: { "timer": ["EvilTower"] }  (单个名字也可直接给字符串)
# =========================================================
@AgentServer.custom_action("ResetTimer")
class ResetTimer(CustomAction):
    def run(self, context, argv):
        try:
            params = json.loads(argv.custom_action_param)
            raw = params.get("timer")

            if isinstance(raw, list):
                targets = raw
            elif raw:
                targets = [raw]
            else:
                targets = []

            dropped = []
            for name in targets:
                if name in TIMER_STORE:
                    del TIMER_STORE[name]
                    dropped.append(name)

            if dropped:
                logger.debug(f"🧹 [计时器作废] {dropped}")
            else:
                logger.debug(f"🧹 [计时器作废跳过] 目标不存在: {targets}")
        except Exception as e:
            # 与 StartTimer 同理: 守卫自己坏掉不该拖累主流程
            logger.error(f"ResetTimer 异常: {e}")
        return True
