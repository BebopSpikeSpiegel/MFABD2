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
# ------------------------------------------------------------------------------
# ⚖️ 两种形态怎么选(本模块最容易写错的地方, 先读这段)
# ------------------------------------------------------------------------------
# 同一个计时器池子上架了两个闸门, 一个是识别器一个是动作。它们**不是同一个东西的两种写法**,
# 而是分别对应两种接线, 互补、不能互相替代。根子在 rec 与 act 的失败语义不对称:
#
#            rec 未通过                          act 返回 False
#   语义     这个候选不选, **让位**给同列表      我被选中了, 但我失败了
#            里后面的候选
#   去向     父节点继续试下一个候选              进**本节点自己**的 on_error
#   打扰     零打扰, 原有流程照走                强行改道
#
# ⇒ act 做不了旁路出口。act 节点得 DirectHit 才能保证每次都执行判断, 而 DirectHit 一定命中、
#   会抢占父节点 next 里排在它后面的所有候选; 未超时时它想「什么都别管、让原流程继续」——
#   act 没有这个通道, 只能 return False 掉进自己的 on_error, 再被迫把原来那几个候选在
#   on_error 里重抄一遍。这是结构性的, 不是配置问题。
# ⇒ rec 也做不了必经门禁。它的「未命中」只是让位, 不触发 on_error, 而那个 on_error 还写在
#   上游节点身上、不在闸门自己身上。
#
# 一句话: **act 是必经点上的二分岔(放行/拦截), rec 是候选列表里的可选分支(认出来就引走,
# 认不出就让位)**。所以:
#
#   链的形状                                    用哪个
#   多节点状态机, 各节点 next 各有一串候选      TimerExpired(功能 A) —— 旁路散挂
#   (爬塔链就是这种, 回边不收敛到一点)          + StartTimer(功能 B) 在入口起表
#   单入口循环, 所有回边独占指回同一个点        TimerGate(功能 D) —— 一个节点顶三个
#
# 为什么没做成「一个参数切换方向」: 反着配(未超时 -> error)意味着每一轮都进错误处理路径,
# 而 error_handling 为真时引擎**不弹回跳栈**, 栈行为会变得极难推。反配有害 ⇒ 只剩一种有
# 意义的配法 ⇒ 参数没有存在意义。两个类各自语义固定。
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
#
# 【功能 D】TimerGate - 循环入口的门禁 (作为 "action" 使用)
# ---------------------------------------------------
# 坐在**单入口循环**的入口上, 一个节点同时承担入口、起表、每轮查表、超时分流四件事,
# 不需要另外挂 StartTimer。语义固定, 无参数可调(理由见开头「两种形态怎么选」):
#
#   无 tag            惰性起表                 -> True  走 next(进循环体)
#   有 tag, 未超时    无                       -> True  走 next
#   有 tag, 已超时    **清零**                 -> False 走 on_error(出循环)
#   minutes<=0        不起表, 视为不限时       -> True  走 next
#   参数坏掉/内部异常  记 error                 -> True  (守卫故障不拦截)
#
# 清零那一步是闭环的关键: 超时退出时把表清掉, 下次再进来就是无 tag 状态、惰性起表重新
# 开始, 不需要额外挂 ResetTimer。
#
# "Xxx_Loop_Gate": {
#     "desc": "循环时间闸。未超时放行进循环体, 超时走 on_error 收尾。这里不写 timeout: 动作
#              失败不经过识别超时窗口(上游 3.1 协议「后继处理」把两条进 on_error 的路径并列
#              写死), 所以 R3「显式 on_error 必配 timeout」在这条路径上不适用。",
#     "action": "Custom",
#     "custom_action": "TimerGate",
#     "custom_action_param": {"timer": "XxxLoop", "minutes": 30},
#     "pre_delay": 0,
#     "post_delay": 0,
#     "next": ["Xxx_Loop_Body"],
#     "on_error": ["Xxx_Loop_End"]
# }
#
# ⚠️ 用它之前必须知道的三条:
#
# 1. **闭环成立的前提是「超时是唯一退出路径」。** 循环体若还有别的出口(战败、任务完成、
#    用户中途停), 那条路不经过 Gate 的清零分支, 表会留到下次——下次进 Gate 立刻判超时、
#    循环一次都不跑, 白白消耗一轮, 第二轮才正常。有其他出口的链, 出口节点挂 ResetTimer。
#
# 2. **Gate 无 recognition = DirectHit 必中**, 放进别人的 next 里会抢占后面所有候选。
#    只能当回边的**独占目标**, 或者放在 next 末尾。这也是它做不了旁路的原因。
#
# 3. **写了 on_error 就丢掉 default_pipeline.json 那条默认的 Global_Null**(R2),
#    收尾语义得自己补全——on_error 里至少要有一个接得住的节点, 否则整条回跳栈作废、
#    task 判失败。
#
# ⚠️ CustomAction **没有 detail 通道**, 功能 A 那招「状态塞进 AnalyzeResult.detail 走
#    maafw.log 而不刷 GUI」在 Gate 上用不了。要看实时进度只能用识别器形态。
#
# ⚠️ 两条**尚未实机复验**的行为, 接第一条链时要盯:
#    · 动作返回 False 后进 on_error 的**时机**——上游文档说不等 timeout, 本项目没验过;
#    · Gate 当回边独占目标时, [JumpBack] 栈的行为是否如预期。
#
#    已知缺陷：
#      计时器tag未根据ID作区分,如果中途断链再回来,动作型的计时会用到上一轮的起点，从而多算时间；
#      识别型同病，但识别型（起、检、复）有3个节点，第二次使用能复位好状态。
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


# =========================================================
# 4. 动作：循环入口的门禁 (未超时放行, 超时清零并拦截)
# 参数: { "timer": "XxxLoop", "minutes": 30 }
#
# 与功能 A(TimerExpired) 的分工见开头「两种形态怎么选」: A 是散挂在候选列表里的旁路,
# 本类是坐在单入口循环入口上的必经门禁。语义固定, 不提供反转参数。
# =========================================================
@AgentServer.custom_action("TimerGate")
class TimerGate(CustomAction):
    def run(self, context, argv):
        try:
            params = json.loads(argv.custom_action_param)
            # 与 TimerExpired 同规矩: 上限该由 pipeline 给, 漏配时放行而不是自作主张定一个数
            try:
                minutes = float(params.get("minutes", 0))
            except (TypeError, ValueError):
                logger.error(f"TimerGate: minutes 参数无法解析({params.get('minutes')!r}), 按不限时处理")
                minutes = 0.0

            # 0 或负数 = 不限时, 恒放行。放在最前面直接返回: 不起表, 一行日志不打。
            if minutes <= 0:
                return True

            name = params.get("timer", "default")
            elapsed = _elapsed(name)  # 无 tag 则就地起表并返回 0 —— Gate 不需要另挂 StartTimer

            if elapsed >= minutes * 60.0:
                # 清零是闭环的关键: 下次再进来就是无 tag 状态, 惰性起表重新开始。
                # ⚠️ 只有走这条路退出才会清。循环体若还有别的出口, 那条路得自己挂 ResetTimer。
                TIMER_STORE.pop(name, None)
                logger.info(f"🛑 [{name}] 已达时间上限 {minutes} 分钟(实际 {_fmt(elapsed)}) → 出循环")
                return False

            # 未超时不打日志: Gate 每轮循环执行一次, 循环体短的话照样能刷屏。
            # CustomAction 没有 detail 通道, 要看实时进度只能用识别器形态(功能 A)。
            return True
        except Exception as e:
            # 与本模块其余守卫一致: 自己坏掉不拦截, 放行让主流程照常跑
            logger.error(f"TimerGate 异常: {e}")
            return True
