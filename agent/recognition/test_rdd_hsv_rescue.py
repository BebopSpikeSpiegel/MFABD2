import unittest

import numpy as np

try:
    from .rdd_hsv_rescue import (
        boxes_agree,
        channel_cutpoints,
        direction_deltas,
        is_strict_mask,
        lineage_parent,
        neighbor_states,
        normalize_rescue_config,
        otsu_threshold,
        select_stable_winner,
        sort_parents,
        strict_profile,
    )
except ImportError:
    from rdd_hsv_rescue import (
        boxes_agree,
        channel_cutpoints,
        direction_deltas,
        is_strict_mask,
        lineage_parent,
        neighbor_states,
        normalize_rescue_config,
        otsu_threshold,
        select_stable_winner,
        sort_parents,
        strict_profile,
    )


RANGES = [
    {"lower": [0, 140, 120], "upper": [12, 255, 255]},
    {"lower": [165, 140, 120], "upper": [180, 255, 255]},
]


class RescueConfigTest(unittest.TestCase):
    def test_default_is_off(self):
        config, error = normalize_rescue_config(None)
        self.assertIsNone(error)
        self.assertEqual(config["mode"], "off")

    def test_invalid_config_fails_closed(self):
        config, error = normalize_rescue_config(
            {"mode": "active", "min_stable_states": 3, "max_full_runs": 2})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

        config, error = normalize_rescue_config(
            {"mode": "active", "max_parents": 100})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

    def test_obsolete_key_is_ignored_not_fatal(self):
        """旧 pipeline 残留的 max_states 不应把救援直接关掉。"""
        config, error = normalize_rescue_config(
            {"mode": "shadow", "max_states": 64})
        self.assertIsNone(error)
        self.assertEqual(config["mode"], "shadow")
        self.assertNotIn("max_states", config)

    def test_strict_profile_only_raises_sv_lower(self):
        profile = strict_profile(RANGES, 12, 18)
        self.assertEqual(profile[0]["lower"], [0, 152, 138])
        self.assertEqual(profile[1]["lower"], [165, 152, 138])
        self.assertEqual(profile[0]["upper"], RANGES[0]["upper"])


class CutpointAndCandidateTest(unittest.TestCase):
    def test_candidate_mask_must_be_true_subset(self):
        baseline = np.array([[1, 1], [0, 1]], dtype=bool)
        same = baseline.copy()
        strict = np.array([[1, 0], [0, 1]], dtype=bool)
        wider = np.array([[1, 1], [1, 1]], dtype=bool)
        self.assertFalse(is_strict_mask(same, baseline))
        self.assertTrue(is_strict_mask(strict, baseline))
        self.assertFalse(is_strict_mask(wider, baseline))

    def test_otsu_splits_two_peaks_and_refuses_degenerate(self):
        two_peaks = np.array([150] * 40 + [220] * 40, dtype=np.uint8)
        cut = otsu_threshold(two_peaks)
        self.assertIsNotNone(cut)
        self.assertTrue(150 < cut <= 220, cut)
        self.assertIsNone(otsu_threshold(np.full(40, 180, dtype=np.uint8)))
        self.assertIsNone(otsu_threshold(np.array([1, 2, 3], dtype=np.uint8)))

    def test_cutpoints_come_from_parent_pixels_and_respect_cap(self):
        """切点由父块自身分布算出；护栏只封顶，不充当工作点。"""
        pixels = np.zeros((80, 3), dtype=np.uint8)
        pixels[:, 1] = [160] * 40 + [230] * 40     # 饱和度双峰
        pixels[:, 2] = [130] * 40 + [210] * 40     # 亮度双峰
        config, _ = normalize_rescue_config({"mode": "shadow"})
        cuts = channel_cutpoints(pixels, RANGES, config)
        self.assertGreater(cuts["亮度"]["增量"], 0)
        self.assertGreater(cuts["饱和"]["增量"], 0)
        self.assertEqual(cuts["亮度"]["基线"], 120)
        self.assertEqual(cuts["饱和"]["基线"], 140)

        tight, _ = normalize_rescue_config(
            {"mode": "shadow", "max_delta_v": 5, "max_delta_s": 5})
        capped = channel_cutpoints(pixels, RANGES, tight)
        self.assertEqual(capped["亮度"]["增量"], 5)
        self.assertEqual(capped["饱和"]["增量"], 5)

    def test_direction_deltas_cover_three_cuts(self):
        cuts = {"饱和": {"增量": 40}, "亮度": {"增量": 50}}
        self.assertEqual(direction_deltas(cuts, "亮度"), (0, 50))
        self.assertEqual(direction_deltas(cuts, "饱和"), (40, 0))
        self.assertEqual(direction_deltas(cuts, "双切"), (40, 50))
        # 某通道无增量时，该方向不成立（避免生成 (0,0) 的空收紧）
        flat = {"饱和": {"增量": 0}, "亮度": {"增量": 0}}
        self.assertIsNone(direction_deltas(flat, "双切"))

    def test_neighbor_states_are_adjacent_and_bounded(self):
        config, _ = normalize_rescue_config({"mode": "shadow"})
        states = neighbor_states("亮度", 0, 50, config)
        self.assertEqual(len(states), 3)
        self.assertEqual({s["si"] for s in states}, {0})
        self.assertEqual([s["vi"] for s in states], [0, 1, 2])
        self.assertEqual([s["delta_v"] for s in states], [45, 50, 55])
        self.assertTrue(all(s["delta_s"] == 0 for s in states))

        # 越过护栏的档位被剔除，不会溢出上限
        tight, _ = normalize_rescue_config(
            {"mode": "shadow", "max_delta_v": 52})
        bounded = neighbor_states("亮度", 0, 50, tight)
        self.assertTrue(all(s["delta_v"] <= 52 for s in bounded))
        self.assertLess(len(bounded), 3)

        # 不同切法落在互不相邻的 si 上，避免跨方向连通成同一分量
        self.assertNotEqual(neighbor_states("亮度", 0, 50, config)[0]["si"],
                            neighbor_states("双切", 40, 50, config)[0]["si"])

    def test_sort_parents_puts_thick_blocks_first(self):
        """短边越大越像红点；实测 boss-adb 的正主(53x16)应排第一。"""
        parents = [
            {"label": 1, "w": 34, "h": 5, "area": 103},
            {"label": 2, "w": 53, "h": 16, "area": 239},
            {"label": 3, "w": 6, "h": 15, "area": 52},
            {"label": 4, "w": 50, "h": 15, "area": 258},
        ]
        order = [p["label"] for p in sort_parents(parents)]
        self.assertEqual(order[0], 2)
        self.assertEqual(order[-1], 1)

    def test_boxes_agree_rejects_scattered_hits(self):
        same = [(10, 10, 12, 12), (10, 11, 12, 12)]
        apart = [(10, 10, 12, 12), (60, 60, 12, 12)]
        self.assertTrue(boxes_agree(same))
        self.assertFalse(boxes_agree(apart))
        self.assertTrue(boxes_agree([(0, 0, 5, 5)]))


class LineageAndStabilityTest(unittest.TestCase):
    def test_lineage_requires_one_eligible_parent(self):
        labels = np.array([[1, 1, 0], [0, 2, 2]], dtype=np.int32)
        child = np.array([[1, 0, 0], [0, 0, 0]], dtype=bool)
        mixed = np.array([[1, 0, 0], [0, 1, 0]], dtype=bool)
        self.assertEqual(lineage_parent(child, labels, [1]), 1)
        self.assertIsNone(lineage_parent(child, labels, [2]))
        self.assertIsNone(lineage_parent(mixed, labels, [1, 2]))

    def test_two_adjacent_states_form_unique_stable_winner(self):
        records = [
            {"parent_id": 1, "box_local": (10, 10, 12, 12),
             "state": {"si": 1, "vi": 0, "delta_s": 5, "delta_v": 0},
             "scan_index": 1},
            {"parent_id": 1, "box_local": (10, 10, 11, 12),
             "state": {"si": 2, "vi": 0, "delta_s": 8, "delta_v": 0},
             "scan_index": 1},
        ]
        winner, reason, support = select_stable_winner(records, 2)
        self.assertEqual(reason, "stable_hit")
        self.assertIs(winner, records[0])
        self.assertEqual(len(support), 1)

    def test_single_state_and_multiple_tracks_fail_closed(self):
        one = [{"parent_id": 1, "box_local": (0, 0, 10, 10),
                "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}}]
        winner, reason, _ = select_stable_winner(one, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "unstable_hit")

        ambiguous = [
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 2, "vi": 0, "delta_s": 2, "delta_v": 0}},
            {"parent_id": 2, "box_local": (20, 20, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 2, "box_local": (20, 20, 10, 10),
             "state": {"si": 2, "vi": 0, "delta_s": 2, "delta_v": 0}},
        ]
        winner, reason, _ = select_stable_winner(ambiguous, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "ambiguous_stable_hits")

    def test_split_in_same_parent_state_is_ambiguous(self):
        records = [
            {"parent_id": 1, "box_local": (0, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
            {"parent_id": 1, "box_local": (20, 0, 10, 10),
             "state": {"si": 1, "vi": 0, "delta_s": 1, "delta_v": 0}},
        ]
        winner, reason, _ = select_stable_winner(records, 2)
        self.assertIsNone(winner)
        self.assertEqual(reason, "ambiguous_split")


if __name__ == "__main__":
    unittest.main()
