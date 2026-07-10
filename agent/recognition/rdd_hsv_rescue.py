"""RedDotDetector 严格 HSV 救援的纯算法工具。

本模块不依赖 MaaFramework，也不负责识别副作用。它只处理：
参数校验、严格 HSV profile、拓扑事件状态、父子 lineage 与跨状态稳定选择。
运行时和离线回放共用这里，避免出现两套救援算法。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


DEFAULT_RESCUE_CONFIG: Dict[str, Any] = {
    "mode": "off",
    "max_delta_s": 48,
    "max_delta_v": 64,
    "max_states": 64,
    "max_full_runs": 8,
    "min_stable_states": 2,
    "time_budget_ms": 40,
}

_MODES = {"off", "shadow", "active"}


class TopologyStates(list):
    """携带状态空间是否因 max_states 被截断的信息。"""

    def __init__(self, values=(), *, truncated=False, total=0):
        super().__init__(values)
        self.truncated = bool(truncated)
        self.total = int(total)


def normalize_rescue_config(raw: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """校验配置。非法配置 fail closed 为 off，并返回原因。"""
    if raw is None:
        return dict(DEFAULT_RESCUE_CONFIG), None
    if not isinstance(raw, dict):
        cfg = dict(DEFAULT_RESCUE_CONFIG)
        return cfg, "flt_hsv_rescue 必须是 object"

    cfg = dict(DEFAULT_RESCUE_CONFIG)
    cfg.update(raw)
    try:
        cfg["mode"] = str(cfg["mode"]).strip().lower()
        if cfg["mode"] not in _MODES:
            raise ValueError("mode 仅支持 off/shadow/active")

        for key in ("max_delta_s", "max_delta_v", "max_states",
                    "max_full_runs", "min_stable_states", "time_budget_ms"):
            cfg[key] = int(cfg[key])

        if cfg["max_delta_s"] <= 0 or cfg["max_delta_v"] <= 0:
            raise ValueError("max_delta_s/v 必须 > 0")
        if cfg["max_states"] < 2:
            raise ValueError("max_states 必须 >= 2")
        if cfg["max_states"] < cfg["min_stable_states"]:
            raise ValueError("max_states 必须 >= min_stable_states")
        if cfg["max_full_runs"] < cfg["min_stable_states"]:
            raise ValueError("max_full_runs 必须 >= min_stable_states")
        if cfg["min_stable_states"] < 2:
            raise ValueError("min_stable_states 必须 >= 2")
        if cfg["time_budget_ms"] <= 0:
            raise ValueError("time_budget_ms 必须 > 0")
        if cfg["max_delta_s"] > 115 or cfg["max_delta_v"] > 135:
            raise ValueError("max_delta_s/v 超过安全上限")
        if cfg["max_states"] > 256:
            raise ValueError("max_states 超过安全上限 256")
        if cfg["max_full_runs"] > 32:
            raise ValueError("max_full_runs 超过安全上限 32")
        if cfg["time_budget_ms"] > 2000:
            raise ValueError("time_budget_ms 超过安全上限 2000")
    except (TypeError, ValueError) as exc:
        disabled = dict(DEFAULT_RESCUE_CONFIG)
        return disabled, str(exc)
    return cfg, None


def strict_profile(
    ranges: Sequence[dict], delta_s: int, delta_v: int,
) -> Optional[List[dict]]:
    """保持 H/upper 不动，仅同步提高所有组的 S/V lower。"""
    if delta_s < 0 or delta_v < 0 or (delta_s == 0 and delta_v == 0):
        return None

    result: List[dict] = []
    for item in ranges:
        lower = item.get("lower") or item.get("lower_hsv")
        upper = item.get("upper") or item.get("upper_hsv")
        if not isinstance(lower, (list, tuple)) or not isinstance(upper, (list, tuple)):
            return None
        if len(lower) != 3 or len(upper) != 3:
            return None
        new_lower = [
            int(lower[0]),
            min(255, int(lower[1]) + int(delta_s)),
            min(255, int(lower[2]) + int(delta_v)),
        ]
        if any(new_lower[i] > int(upper[i]) for i in range(3)):
            return None
        result.append({"lower": new_lower, "upper": [int(v) for v in upper]})
    return result or None


def is_strict_mask(candidate: np.ndarray, baseline: np.ndarray) -> bool:
    """candidate 必须是 baseline 的真子集。"""
    if candidate.shape != baseline.shape:
        return False
    if np.any(candidate & ~baseline):
        return False
    return not np.array_equal(candidate, baseline)


def mask_digest(mask: np.ndarray) -> str:
    return hashlib.sha1(np.ascontiguousarray(mask).view(np.uint8)).hexdigest()


def _events(values: np.ndarray, bases: Iterable[int], maximum: int) -> List[int]:
    events = {0}
    if values.size == 0:
        return [0]
    for base in bases:
        margins = values.astype(np.int16) - int(base) + 1
        for value in np.unique(margins):
            ivalue = int(value)
            if 0 < ivalue <= maximum:
                events.add(ivalue)
    return sorted(events)


def build_topology_states(
    hsv_np: np.ndarray,
    eligible_mask: np.ndarray,
    ranges: Sequence[dict],
    config: Dict[str, Any],
) -> List[Dict[str, int]]:
    """从父 blob 实际 S/V 值生成有界二维拓扑事件状态。"""
    pixels = hsv_np[eligible_mask]
    if pixels.size == 0:
        return []

    s_bases, v_bases = [], []
    for item in ranges:
        lower = item.get("lower") or item.get("lower_hsv")
        if isinstance(lower, (list, tuple)) and len(lower) == 3:
            s_bases.append(int(lower[1]))
            v_bases.append(int(lower[2]))
    if not s_bases:
        return []

    s_events = _events(pixels[:, 1], s_bases, config["max_delta_s"])
    v_events = _events(pixels[:, 2], v_bases, config["max_delta_v"])
    max_s = max(1, config["max_delta_s"])
    max_v = max(1, config["max_delta_v"])

    states = []
    for si, ds in enumerate(s_events):
        for vi, dv in enumerate(v_events):
            if ds == 0 and dv == 0:
                continue
            # 先试最小的归一化收紧，再用总增量保证确定性。
            cost = max(ds / max_s, dv / max_v)
            states.append({
                "si": si, "vi": vi, "delta_s": ds, "delta_v": dv,
                "_cost": cost,
            })
    states.sort(key=lambda x: (x["_cost"], x["delta_s"] + x["delta_v"],
                               x["delta_s"], x["delta_v"]))
    total = len(states)
    truncated = total > config["max_states"]
    states = states[:config["max_states"]]
    for state in states:
        state.pop("_cost", None)
    return TopologyStates(states, truncated=truncated, total=total)


def lineage_parent(
    blob_mask: np.ndarray,
    baseline_labels: np.ndarray,
    eligible_parent_ids: Iterable[int],
) -> Optional[int]:
    """候选必须完全来自唯一的 eligible baseline 父 blob。"""
    labels = set(int(v) for v in np.unique(baseline_labels[blob_mask]))
    labels.discard(0)
    eligible = set(int(v) for v in eligible_parent_ids)
    if len(labels) != 1:
        return None
    parent = next(iter(labels))
    return parent if parent in eligible else None


def _box_iou(a: Sequence[int], b: Sequence[int]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _center_distance(a: Sequence[int], b: Sequence[int]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    return math.hypot((ax + aw / 2) - (bx + bw / 2),
                      (ay + ah / 2) - (by + bh / 2))


def _adjacent(a: dict, b: dict) -> bool:
    return abs(a["si"] - b["si"]) + abs(a["vi"] - b["vi"]) == 1


def select_stable_winner(
    records: Sequence[dict],
    min_stable_states: int,
    min_iou: float = 0.5,
    max_center_shift: float = 2.5,
) -> Tuple[Optional[dict], str, List[dict]]:
    """以相邻拓扑状态组成稳定图；唯一稳定分量才允许 winner。"""
    if not records:
        return None, "no_hit", []

    per_state: Dict[Tuple[int, int, int], int] = {}
    for record in records:
        key = (int(record["parent_id"]), int(record["state"]["si"]),
               int(record["state"]["vi"]))
        per_state[key] = per_state.get(key, 0) + 1
    if any(count > 1 for count in per_state.values()):
        return None, "ambiguous_split", []

    graph = [set() for _ in records]
    for i, left in enumerate(records):
        for j in range(i + 1, len(records)):
            right = records[j]
            if left["parent_id"] != right["parent_id"]:
                continue
            if not _adjacent(left["state"], right["state"]):
                continue
            if _box_iou(left["box_local"], right["box_local"]) < min_iou:
                continue
            if _center_distance(left["box_local"], right["box_local"]) > max_center_shift:
                continue
            graph[i].add(j)
            graph[j].add(i)

    components, seen = [], set()
    for start in range(len(records)):
        if start in seen:
            continue
        stack, indexes = [start], []
        seen.add(start)
        while stack:
            cur = stack.pop()
            indexes.append(cur)
            for nxt in graph[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        states = {(records[i]["state"]["si"], records[i]["state"]["vi"]) for i in indexes}
        if len(states) >= min_stable_states:
            components.append(indexes)

    support = [{
        "parent_id": records[idxs[0]]["parent_id"],
        "states": [records[i]["state"] for i in idxs],
        "boxes": [list(records[i]["box_local"]) for i in idxs],
    } for idxs in components]

    if not components:
        return None, "unstable_hit", support
    if len(components) != 1:
        return None, "ambiguous_stable_hits", support

    chosen = min(
        (records[i] for i in components[0]),
        key=lambda r: (r["state"]["delta_s"] + r["state"]["delta_v"],
                       r["state"]["delta_s"], r["state"]["delta_v"],
                       r.get("scan_index", 0)),
    )
    return chosen, "stable_hit", support
