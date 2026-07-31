# -*- coding: utf-8 -*-
"""Pipeline 动态管理器 —— 运行时改写 / 还原 MaaFramework 管线节点。"""

import copy
import json
import re

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from maa.context import Context

import utils
from recognition.counter import TAG_STORE

__version__ = "2.0.0"

# ==============================================================================
# Pipeline 动态管理器  v2.0.0
# ==============================================================================
# 提供两个 Custom Action：
#
#   PatchPipeline    运行时改写节点参数，并自动登记还原点
#   RestorePipeline  把改过的节点还原回去
#
# ------------------------------------------------------------------------------
# 设计要点：为什么不用手写 origin
# ------------------------------------------------------------------------------
# 旧版要求调用方在 JSON 里手抄一份 "origin"（原值）用于还原。这有三个问题：抄错了
# 无人校验；抄的是磁盘上的静态值，而运行时真实生效的值还叠加了 UI 选项覆盖；节点改了
# 之后 origin 不会跟着改。
#
# 本版改为：打补丁前后各向框架查询一次节点的**当前定义**，两次结果做差，差出来的部分
# 就是还原点。框架是唯一权威 —— 取值、V1/V2 格式转译、字段合并全部由它完成，本模块
# 只做纯字典比较。调用方一个字都不用写。
#
# ------------------------------------------------------------------------------
# 关于写法：V1 / V2 都行
# ------------------------------------------------------------------------------
# 下面的示例用 V1 扁平写法（recognition / action 直接是字符串，参数摊在节点顶层），
# 因为这是本项目的习惯。但**两种写法本模块都吃**：
#
#   V1  "action": "Custom", "custom_action": "PatchPipeline", "custom_action_param": {...}
#   V2  "action": { "type": "Custom", "param": { "custom_action": "PatchPipeline",
#                                                "custom_action_param": {...} } }
#
# patch 内容同理 —— 写 V1 的 "expected": [...]，或写 V2 的
# "recognition": {"param": {"expected": [...]}}，都可以。框架自己完成转译，本模块
# 不解析这些字段，只把补丁原样交给框架。
#
# ------------------------------------------------------------------------------
# 1. 打补丁：一个节点
# ------------------------------------------------------------------------------
# "action": "Custom",
# "custom_action": "PatchPipeline",
# "custom_action_param": {
#     "target": "Battle_Entry",              // [必填] 目标节点名
#     "patch":  { "timeout": 30000 }         // [必填] 要改的字段，按平时写节点的写法写
# }
#
# ------------------------------------------------------------------------------
# 2. 打补丁：一批节点用同一份补丁
# ------------------------------------------------------------------------------
# "custom_action_param": {
#     "target": ["Node_A", "Node_B"],        // 数组
#     "patch":  { "enabled": false }
# }
#
# 也可以用正则选目标（在框架已加载的节点名里搜，支持字符串或数组）：
# "custom_action_param": {
#     "target": { "regex": "^Shop_Buy_Item_\\d+$" },
#     "patch":  { "timeout": 5000 }
# }
#
# ⚠️ 正则零命中 = 硬失败。宁可报错，也不要让"以为改了其实没改"悄悄溜过去。
#
# ------------------------------------------------------------------------------
# 3. 打补丁：每个节点各自不同的补丁
# ------------------------------------------------------------------------------
# target + patch 是「同一份补丁打到一批节点」。如果每个节点要改的东西不一样，用 patches：
#
# "custom_action_param": {
#     "patches": {
#         "Node_A": { "next": ["Node_X"] },
#         "Node_B": { "timeout": 3000 }
#     }
# }
#
# 之所以要有这个形态：一个节点只能有一种 action，没法靠"多写几个节点"来表达。
# target+patch 与 patches 同时出现 = 硬失败（意图不明，不猜）。
#
# ------------------------------------------------------------------------------
# 4. 还原
# ------------------------------------------------------------------------------
# "custom_action": "RestorePipeline",
# "custom_action_param": { "target": "Battle_Entry" }      // 单个
# "custom_action_param": { "target": ["Node_A","Node_B"] } // 多个
# "custom_action_param": { "target": "*" }                 // 本次任务改过的全部
#
# 还原点由 PatchPipeline 自动登记，采用「先到先得」：同一节点被反复打补丁时，还原点
# 始终是**第一次打补丁之前**的状态。
#
# 还原本身是幂等的 —— 目标不在账本里（没被改过 / 已经还原过）只发一条告警，不算失败。
#
# ------------------------------------------------------------------------------
# 5. 旁作用：顺手清计数器、顺手点一下
# ------------------------------------------------------------------------------
# 两个动作都支持。同样是因为一个节点只能有一种 action —— 想「改参数 + 点一下 +
# 清计数器」，不做成参数就得拆三个节点。
#
# "custom_action_param": {
#     "target": "Battle_Entry",
#     "patch":  { "timeout": 30000 },
#     "reset_tags": ["Battle_Count"],            // 把这些计数器清零
#     "click": {}                                // 点当前识别框中心
#     // "click": { "offset": [10, 20, 40, 40] } // 或点 [识别框左上角+dx+dy] 起、w×h 的区域中心
# }
#
# ------------------------------------------------------------------------------
# 6. 新建节点
# ------------------------------------------------------------------------------
# 补丁也可以凭空造一个资源里不存在的节点，但必须显式声明 —— 因为「节点名拼错」和
# 「打算新建」在接口层长得一模一样，不声明就无从区分：
#
# "custom_action_param": {
#     "target": "Tmp_Node",
#     "create": true,                        // 不写 = 节点不存在即报错
#     "patch":  { "recognition": "DirectHit", "action": "Click", "target": [1,2,3,4] }
# }
#
# 新建的节点没有"原状"可还原，因此不进账本；任务结束时框架会自动清掉它。
#
# ------------------------------------------------------------------------------
# 7. 占位符：$box / $self / $caller
# ------------------------------------------------------------------------------
# 可以出现在 patch 里的任意位置（不限于某个字段）：
#
#   "$box"     整条替换成当前识别框 [x, y, w, h]
#   "$self"    子串替换成"正在被改的那个节点的名字"（配合正则选择器时有用）
#   "$caller"  子串替换成 caller 参数的值，需要在参数里显式写 "caller": "本节点名"
#
# "custom_action_param": {
#     "caller": "My_Entry_Node",
#     "target": { "regex": "^Enemy_.*$" },
#     "patch":  { "roi": "$box", "next": ["[JumpBack]$caller"] }
# }
#
# ------------------------------------------------------------------------------
# 8. 三个容易踩的坑
# ------------------------------------------------------------------------------
# (1) 数组是整体替换，不是逐元素合并。
#     想改 "all_of" 里第 0 个元素的 roi，必须把整个 all_of 数组写全，只写第 0 个会
#     让其余元素消失。（把条件写成外置节点、在 all_of 里只放节点名，就能绕开这点 ——
#     那时 roi 是被引用节点的一级字段，直接 patch 即可。）
#
# (2) custom_action_param / custom_recognition_param 是整体替换。
#     框架不理解这两个字段的内部结构，所以不做合并。补丁里带上它们就必须写全量，
#     只写想改的那一个键会让其余键丢失。
#
# (3) 补丁的作用范围是**单次任务**。
#     任务结束后框架会自动丢弃所有运行时改写，还原点账本也随之失效 —— 不需要、也
#     无法跨任务还原。
# ==============================================================================


class ConfigError(Exception):
    """调用方在 JSON 里写错了。硬失败走 on_error，不静默降级。"""


# ------------------------------------------------------------------------------
# 还原点账本
# ------------------------------------------------------------------------------
# 按 (tasker 句柄, 任务号) 分桶：运行时改写本就随任务结束失效，账本必须同生共死，
# 否则下一个任务还原时会把陈旧的还原点灌进一个根本没被改过的节点。
# 分桶同时让多账号并行互不串号。
_LEDGERS: "dict[tuple[int, int], dict[str, dict]]" = {}

_MISSING = object()

# 框架对这两个字段是整体替换，不做合并 —— 它不理解里面的结构，所以不敢合并。
# 因此求差时**绝不能递归进去**：只改了其中一个键，也必须把整份旧值记下来，
# 否则还原时会把没改过的兄弟键一起抹掉。
_ATOMIC_KEYS = frozenset({"custom_action_param", "custom_recognition_param"})


_DEGRADED_KEY_WARNED = False


def _tasker_key(context: Context) -> int:
    """tasker 的稳定标识 —— 取框架侧的 C 句柄，不要用 id()。

    binding 每次自定义动作回调都 `Context(c_context)` 新建上下文，而 Context.__init__
    里又 `Tasker(handle=...)` 新建一个包装对象（maa/context.py::_init_tasker）。
    `id(context.tasker)` 拿到的只是这个**短命包装**的内存地址：打补丁和还原是两次独立
    回调，地址通常对不上 —— 还原会查到空账本，打一条 warning 然后静默变成空操作，节点
    永远停在被改写的状态。顺带地，账本的陈旧桶清理条件也永远匹配不上，_LEDGERS 只增不减。

    `_handle` 是 MaaContextGetTasker 的返回值，即框架侧 tasker 对象的地址，在该 tasker
    的整个生命周期内恒定，才是真正的身份。它是 binding 的私有属性，但没有等价的公开
    接口；一旦哪天取不到，下面会退化成单桶并告警，而不是悄悄退回不可靠的 id()。
    """
    global _DEGRADED_KEY_WARNED
    handle = getattr(context.tasker, "_handle", None)
    # restype 为 c_void_p 时 ctypes 直接给出 int；万一将来包成 ctypes 对象，取 .value
    handle = getattr(handle, "value", handle)
    try:
        key = int(handle or 0)
    except (TypeError, ValueError):
        key = 0
    if not key and not _DEGRADED_KEY_WARNED:
        _DEGRADED_KEY_WARNED = True
        utils.mfaalog.warning(
            "[Py] ⚠️ 取不到 tasker 句柄，还原点账本退化为单桶"
            "（一实例一 agent 进程下仍安全，多 tasker 共进程才会串号）"
        )
    return key


def _ledger(context: Context, argv: CustomAction.RunArg) -> dict:
    tasker_id = _tasker_key(context)
    task_id = argv.task_detail.task_id
    for stale in [k for k in _LEDGERS if k[0] == tasker_id and k[1] != task_id]:
        del _LEDGERS[stale]
    return _LEDGERS.setdefault((tasker_id, task_id), {})


def _diff(before, after, prefix=()):
    """列出 before -> after 变化的叶子路径，返回 [(路径, 旧值)]。

    字典递归下去，其余类型（含数组）整体比较 —— 框架对数组就是整体替换。
    原子字段（见 _ATOMIC_KEYS）同样整体比较，不递归。
    """
    out = []
    atomic = bool(prefix) and prefix[-1] in _ATOMIC_KEYS
    if not atomic and isinstance(before, dict) and isinstance(after, dict):
        for key in set(before) | set(after):
            out += _diff(before.get(key, _MISSING), after.get(key, _MISSING), prefix + (key,))
    elif before is not _MISSING or after is not _MISSING:
        if before != after:
            out.append((prefix, before))
    return out


def _build_restore(before: dict, after: dict):
    """把 diff 结果反推成最小还原载荷。返回 (载荷, 被跳过的新增路径)。"""
    payload: dict = {}
    skipped = []
    for path, old in _diff(before, after):
        if old is _MISSING:
            # 补丁新增了原本不存在的字段 —— 没有"原状"可回退，交给任务结束时自动失效
            skipped.append(".".join(str(p) for p in path))
            continue
        cursor = payload
        for seg in path[:-1]:
            cursor = cursor.setdefault(seg, {})
        cursor[path[-1]] = old
    return payload, skipped


# ------------------------------------------------------------------------------
# 参数与占位符
# ------------------------------------------------------------------------------

def _parse_param(argv: CustomAction.RunArg) -> dict:
    raw = argv.custom_action_param
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw).strip())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"custom_action_param 不是合法 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise ConfigError(f"custom_action_param 必须是对象，实际是 {type(parsed).__name__}")
    return parsed


def _box_of(argv: CustomAction.RunArg):
    box = getattr(argv, "box", None)
    if not box or int(getattr(box, "w", 0)) <= 0 or int(getattr(box, "h", 0)) <= 0:
        return None
    return [int(box.x), int(box.y), int(box.w), int(box.h)]


def _resolve(data, node: str, caller: str, box):
    """把 patch 里的占位符替换掉。"""
    if isinstance(data, str):
        if data == "$box":
            if box is None:
                raise ConfigError(f"节点 [{node}] 的补丁用了 $box，但当前没有有效识别框")
            return list(box)
        if "$caller" in data and not caller:
            raise ConfigError(f"节点 [{node}] 的补丁用了 $caller，但参数里没写 caller")
        out = data.replace("$self", node)
        return out.replace("$caller", caller) if caller else out
    if isinstance(data, list):
        return [_resolve(x, node, caller, box) for x in data]
    if isinstance(data, dict):
        return {k: _resolve(v, node, caller, box) for k, v in data.items()}
    return data


def _targets(context: Context, spec, *, allow_regex: bool) -> "list[str]":
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, list):
        if not spec:
            raise ConfigError("target 是空数组")
        return [str(x) for x in spec]
    if isinstance(spec, dict) and "regex" in spec:
        if not allow_regex:
            raise ConfigError("RestorePipeline 的 target 不支持 regex，请写节点名或 \"*\"")
        raw = spec["regex"]
        patterns = [raw] if isinstance(raw, str) else list(raw)
        try:
            compiled = [re.compile(p) for p in patterns]
        except re.error as exc:
            raise ConfigError(f"target.regex 不是合法正则：{exc}") from None
        hit = [n for n in context.tasker.resource.node_list if any(c.search(n) for c in compiled)]
        if not hit:
            raise ConfigError(f"target.regex {patterns} 没有匹配到任何节点")
        return hit
    raise ConfigError(f"target 必须是节点名 / 数组 / {{\"regex\": ...}}，实际是 {spec!r}")


# ------------------------------------------------------------------------------
# 旁作用
# ------------------------------------------------------------------------------

def _side_reset_tags(params: dict) -> None:
    raw = params.get("reset_tags")
    if not raw:
        return
    tags = raw if isinstance(raw, list) else [raw]
    for tag in tags:
        TAG_STORE[tag] = 0
    utils.mfaalog.info(f"[Py] 🧹 [旁作用] 计数器已清零: {list(tags)}")


def _side_click(context: Context, argv: CustomAction.RunArg, spec) -> None:
    if spec is None:
        return
    box = _box_of(argv)
    if box is None:
        raise ConfigError("声明了 click 旁作用，但当前没有有效识别框")
    x, y, w, h = box
    offset = (spec or {}).get("offset") if isinstance(spec, dict) else None
    if offset:
        if not isinstance(offset, list) or len(offset) != 4:
            raise ConfigError(f"click.offset 必须是 4 元数组 [dx, dy, w, h]，实际是 {offset!r}")
        dx, dy, ow, oh = (int(v) for v in offset)
        cx, cy = x + dx + ow / 2, y + dy + oh / 2
    else:
        cx, cy = x + w / 2, y + h / 2
    context.tasker.controller.post_click(int(cx), int(cy))
    utils.mfaalog.info(f"[Py] 🖱️ [旁作用] 点击 ({int(cx)}, {int(cy)})")


# ------------------------------------------------------------------------------
# PatchPipeline
# ------------------------------------------------------------------------------

@AgentServer.custom_action("PatchPipeline")
class PatchPipeline(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _parse_param(argv)
            _side_reset_tags(params)

            has_pairs = "patches" in params
            has_single = "target" in params or "patch" in params
            if has_pairs and has_single:
                raise ConfigError("target/patch 与 patches 不能同时出现，请二选一")

            if has_pairs:
                pairs = params["patches"]
                if not isinstance(pairs, dict) or not pairs:
                    raise ConfigError("patches 必须是非空的 {节点名: 补丁} 对象")
                plan = list(pairs.items())
            else:
                if "target" not in params:
                    raise ConfigError("缺少 target（或改用 patches）")
                if "patch" not in params:
                    raise ConfigError("缺少 patch（或改用 patches）")
                patch = params["patch"]
                if not isinstance(patch, dict):
                    raise ConfigError(f"patch 必须是对象，实际是 {type(patch).__name__}")
                plan = [(n, patch) for n in _targets(context, params["target"], allow_regex=True)]

            backup = params.get("backup", True)
            create = bool(params.get("create", False))
            caller = params.get("caller", "")
            box = _box_of(argv)
            ledger = _ledger(context, argv)

            patched, created, recorded = [], [], 0
            for node, raw_patch in plan:
                before = context.get_node_data(node)
                if before is None and not create:
                    raise ConfigError(
                        f"节点 [{node}] 不存在（名字拼错？若确实想新建，请加 \"create\": true）"
                    )

                payload = _resolve(copy.deepcopy(raw_patch), node, caller, box)
                if not context.override_pipeline({node: payload}):
                    raise ConfigError(f"节点 [{node}] 的补丁被框架拒绝，请检查字段名与取值")

                if before is None:
                    created.append(node)
                    continue
                patched.append(node)

                # 先到先得：还原点永远是第一次打补丁之前的状态
                if backup and node not in ledger:
                    after = context.get_node_data(node) or {}
                    restore, skipped = _build_restore(before, after)
                    if restore:
                        ledger[node] = restore
                        recorded += 1
                    if skipped:
                        utils.mfaalog.info(
                            f"[Py] ℹ️ [{node}] 新增字段无原状可还原，随任务结束自动失效: {skipped}"
                        )

            if patched:
                utils.mfaalog.info(
                    f"[Py] 🔧 已改写 {len(patched)} 个节点（登记还原点 {recorded} 个）: "
                    f"{patched if len(patched) <= 6 else patched[:6] + ['...']}"
                )
            if created:
                utils.mfaalog.info(f"[Py] ✨ 新建节点 {len(created)} 个（不进账本）: {created}")

            _side_click(context, argv, params.get("click"))
            return True

        except ConfigError as exc:
            utils.mfaalog.error(f"[Py] ❌ PatchPipeline 配置错误: {exc}")
            return False
        except Exception as exc:
            import traceback
            utils.mfaalog.error(f"[Py] 💥 PatchPipeline 运行异常: {exc}")
            utils.mfaalog.error(traceback.format_exc())
            return False


# ------------------------------------------------------------------------------
# RestorePipeline
# ------------------------------------------------------------------------------

@AgentServer.custom_action("RestorePipeline")
class RestorePipeline(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        try:
            params = _parse_param(argv)
            _side_reset_tags(params)

            if "target" not in params:
                raise ConfigError("缺少 target（节点名 / 数组 / \"*\"）")

            ledger = _ledger(context, argv)
            spec = params["target"]
            if spec == "*":
                targets = list(ledger.keys())
                if not targets:
                    utils.mfaalog.info("[Py] 🧹 本次任务没有需要还原的节点")
                    _side_click(context, argv, params.get("click"))
                    return True
            else:
                targets = _targets(context, spec, allow_regex=False)

            done, missing = [], []
            for node in targets:
                payload = ledger.get(node)
                if payload is None:
                    # 没被改过 / 已还原过 / 是新建的节点 —— 还原是幂等的，不算失败
                    missing.append(node)
                    continue
                if not context.override_pipeline({node: payload}):
                    raise ConfigError(f"节点 [{node}] 的还原被框架拒绝")
                # 销账必须在还原成功之后：先 pop 再失败会让还原点永久丢失，
                # 节点既停在被改写状态、又再也还原不回来（连 "*" 也查不到）
                ledger.pop(node, None)
                done.append(node)

            if done:
                utils.mfaalog.info(f"[Py] 🔙 已还原 {len(done)} 个节点: {done}")
            if missing:
                utils.mfaalog.warning(f"[Py] ⚠️ 账本中无还原点，已跳过（不算失败）: {missing}")

            _side_click(context, argv, params.get("click"))
            return True

        except ConfigError as exc:
            utils.mfaalog.error(f"[Py] ❌ RestorePipeline 配置错误: {exc}")
            return False
        except Exception as exc:
            import traceback
            utils.mfaalog.error(f"[Py] 💥 RestorePipeline 运行异常: {exc}")
            utils.mfaalog.error(traceback.format_exc())
            return False
