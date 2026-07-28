"""只读回放 RedDotDetector_samples 的台账(samples.jsonl.log，兼容旧名 samples.jsonl)。

运行示例（从仓库根目录）：
    python agent/recognition/replay_rdd_samples.py assets/debug/RedDotDetector_samples
    python agent/recognition/replay_rdd_samples.py assets/debug/RedDotDetector_samples \
        --rescue --expect-rescue-node Pass_SelectPass
"""

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image


HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_ROOT = os.path.dirname(HERE)
if AGENT_ROOT not in sys.path:
    sys.path.insert(0, AGENT_ROOT)

from recognition.binarymatch import (  # noqa: E402
    RedDotDetector,
    _FLT_ASPECT_DEFAULT,
)
from recognition.rdd_hsv_rescue import normalize_rescue_config  # noqa: E402
from recognition.rdd_sampler import MANIFEST_NAMES  # noqa: E402


def _load_entries(sample_dir):
    """读齐目录内所有台账。旧名在前(产生更早)，同目录两名并存时合并而非二选一。"""
    entries, used = [], []
    for name in reversed(MANIFEST_NAMES):
        path = os.path.join(sample_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream if line.strip()]
        entries.extend(rows)
        used.append(f"{name}({len(rows)})")
    if not used:
        raise SystemExit(
            f"{sample_dir} 下没有台账，找过：{'、'.join(MANIFEST_NAMES)}")
    print(f"台账：{'、'.join(used)}", file=sys.stderr)
    return entries


def _load_crop(path):
    rgb = np.array(Image.open(path).convert("RGB"))
    bgr = rgb[..., ::-1]
    return np.array(Image.fromarray(bgr[..., ::-1]).convert("HSV"))


def _recorded_rescue(entry):
    """兼容两代键名：`救援`(现行，与 detail 金字塔同为中文键)与 `rescue`(2026-07 的 3 条旧记录)。"""
    return entry.get("救援") or entry.get("rescue") or {}


def _expected_local(entry):
    box = entry.get("box")
    if not box:
        return None
    if entry.get("mode") == "standalone":
        roi = entry.get("roi") or [0, 0, 0, 0]
        return [box[0] - roi[0], box[1] - roi[1], box[2], box[3]]
    return list(box)


def replay(sample_dir, rescue=False, expected_rescue_nodes=()):
    detector = RedDotDetector()
    total = parity = box_parity = rescue_stable = rescue_trigger = 0
    rescue_checks = rescue_pass = skipped_no_crop = 0
    checked_rescue_nodes = set()
    mismatches = []

    entries = _load_entries(sample_dir)

    fallback_rescue = {
        "mode": "shadow",
        "max_delta_s": 48,
        "max_delta_v": 64,
        "max_states": 128,
        "max_full_runs": 24,
        "min_stable_states": 2,
        "time_budget_ms": 1000,
    }
    expected_rescue_nodes = set(expected_rescue_nodes or [])

    for index, entry in enumerate(entries, 1):
        crop_name = next(
            (name for name in entry.get("files", []) if name.endswith("_roi_crop.png")),
            None,
        )
        if not crop_name:
            skipped_no_crop += 1
            mismatches.append({
                "line": index, "node": entry.get("node"),
                "error": "missing roi_crop",
            })
            continue
        hsv_np = _load_crop(os.path.join(sample_dir, crop_name))
        params = entry.get("params") or {}
        hsv_ranges = (params.get("configured_hsv_ranges")
                      or params["hsv_ranges"])
        area_min, area_max = params.get("red_area", [30, 1200])
        asp_lo, asp_hi = params.get("flt_aspect", _FLT_ASPECT_DEFAULT)
        min_conf = params.get("min_conf", 0.55)
        gap_ratio = params.get("gap_ratio", 0.35)
        outcome = detector._detect_once(
            hsv_np, hsv_ranges, area_min, area_max, asp_lo, asp_hi,
            gap_ratio, min_conf, 0, 0,
        )
        stage, _ = detector._diagnose(outcome["stat"], area_min, area_max, min_conf)
        expected_hit = entry.get("result") == "hit"
        total += 1

        rescue_result = None
        stable = False
        if rescue and not outcome["hit"] and stage == "aspect":
            rescue_trigger += 1
            raw_rescue = dict(params.get("flt_hsv_rescue") or fallback_rescue)
            raw_rescue["mode"] = "shadow"
            rescue_cfg, rescue_error = normalize_rescue_config(raw_rescue)
            if rescue_error:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "rescue_config_error": rescue_error,
                })
                continue
            rescue_result = detector._run_hsv_rescue(
                hsv_np=hsv_np, baseline=outcome, hsv_ranges=hsv_ranges,
                area_range=(area_min, area_max),
                aspect_range=(asp_lo, asp_hi),
                gap_ratio=gap_ratio, min_conf=min_conf, rx=0, ry=0,
                config=rescue_cfg,
            )
            stable = rescue_result.get("_decision") == "stable_hit"
            if stable:
                rescue_stable += 1

        expected_rescue = entry.get("expected_rescue")
        if (expected_rescue is None and entry.get("node") in expected_rescue_nodes
                and not outcome["hit"] and stage == "aspect"):
            expected_rescue = True
        recorded_rescue = _recorded_rescue(entry)
        recorded_mode = recorded_rescue.get("模式") or recorded_rescue.get("mode")
        recorded_active = (recorded_mode == "active"
                           and expected_hit and not outcome["hit"])
        # 只在"旧版 active 曾经救回过"时才要求新版也救回。反向不成立：
        # 旧算法救不出，不等于新算法不该救出 —— 那正是迭代要改进的部分。
        # （2026-07-11 的 2 条 active 记录即属此类：旧网格爬失败，新直方图定点成功。）
        if (expected_rescue is None and recorded_mode == "active"
                and not outcome["hit"] and expected_hit):
            expected_rescue = True
        if expected_rescue is not None:
            rescue_checks += 1
            checked_rescue_nodes.add(entry.get("node"))
            if stable == bool(expected_rescue):
                rescue_pass += 1
            else:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "expected_rescue": bool(expected_rescue),
                    "actual_rescue": stable,
                    "decision": (rescue_result or {}).get("_decision"),
                    "stop_reason": (rescue_result or {}).get("停因"),
                })
            if recorded_active and stable:
                winner = rescue_result.get("_winner")
                actual_box = (list(winner["candidate"]["box_local"])
                              if winner is not None else None)
                expected_box = _expected_local(entry)
                if actual_box != expected_box:
                    mismatches.append({
                        "line": index, "node": entry.get("node"),
                        "expected_active_box": expected_box,
                        "actual_active_box": actual_box,
                    })
                actual_profile = (winner.get("profile")
                                  if winner is not None else None)
                expected_profile = params.get("effective_hsv_ranges")
                if expected_profile is not None and actual_profile != expected_profile:
                    mismatches.append({
                        "line": index, "node": entry.get("node"),
                        "expected_active_profile": expected_profile,
                        "actual_active_profile": actual_profile,
                    })

        expected_baseline_hit = False if recorded_active else expected_hit
        if outcome["hit"] == expected_baseline_hit:
            parity += 1
        else:
            mismatches.append({
                "line": index, "node": entry.get("node"),
                "expected_baseline": "hit" if expected_baseline_hit else "miss",
                "actual": "hit" if outcome["hit"] else stage,
            })
            continue

        if expected_hit and not recorded_active:
            actual_box = list(outcome["candidates"][0]["box_local"])
            expected_box = _expected_local(entry)
            if actual_box == expected_box:
                box_parity += 1
            else:
                mismatches.append({
                    "line": index, "node": entry.get("node"),
                    "expected_box": expected_box, "actual_box": actual_box,
                })

    if total == 0:
        mismatches.append({"error": "no replayable samples"})
    for node in expected_rescue_nodes - checked_rescue_nodes:
        mismatches.append({
            "node": node,
            "error": "no aspect-stage rescue sample was checked",
        })

    print(json.dumps({
        "total": total,
        "result_parity": parity,
        "hit_box_parity": box_parity,
        "skipped_no_crop": skipped_no_crop,
        "rescue_trigger": rescue_trigger,
        "rescue_stable": rescue_stable,
        "rescue_checks": rescue_checks,
        "rescue_pass": rescue_pass,
        "mismatches": mismatches,
    }, ensure_ascii=False, indent=2))
    return 0 if not mismatches else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sample_dir")
    parser.add_argument("--rescue", action="store_true")
    parser.add_argument("--expect-rescue-node", action="append", default=[])
    args = parser.parse_args()
    raise SystemExit(replay(
        os.path.abspath(args.sample_dir), args.rescue, args.expect_rescue_node))
