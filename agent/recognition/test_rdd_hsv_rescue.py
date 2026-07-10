import unittest

import numpy as np

try:
    from .rdd_hsv_rescue import (
        build_topology_states,
        is_strict_mask,
        lineage_parent,
        normalize_rescue_config,
        select_stable_winner,
        strict_profile,
    )
except ImportError:
    from rdd_hsv_rescue import (
        build_topology_states,
        is_strict_mask,
        lineage_parent,
        normalize_rescue_config,
        select_stable_winner,
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
            {"mode": "active", "max_states": 10000})
        self.assertIsNotNone(error)
        self.assertEqual(config["mode"], "off")

    def test_strict_profile_only_raises_sv_lower(self):
        profile = strict_profile(RANGES, 12, 18)
        self.assertEqual(profile[0]["lower"], [0, 152, 138])
        self.assertEqual(profile[1]["lower"], [165, 152, 138])
        self.assertEqual(profile[0]["upper"], RANGES[0]["upper"])


class TopologyStateTest(unittest.TestCase):
    def test_candidate_mask_must_be_true_subset(self):
        baseline = np.array([[1, 1], [0, 1]], dtype=bool)
        same = baseline.copy()
        strict = np.array([[1, 0], [0, 1]], dtype=bool)
        wider = np.array([[1, 1], [1, 1]], dtype=bool)
        self.assertFalse(is_strict_mask(same, baseline))
        self.assertTrue(is_strict_mask(strict, baseline))
        self.assertFalse(is_strict_mask(wider, baseline))

    def test_states_come_from_observed_sv_events(self):
        hsv = np.zeros((2, 2, 3), dtype=np.uint8)
        hsv[..., 1] = [[140, 149], [160, 180]]
        hsv[..., 2] = [[120, 129], [150, 180]]
        eligible = np.ones((2, 2), dtype=bool)
        config, _ = normalize_rescue_config({
            "mode": "shadow", "max_delta_s": 30, "max_delta_v": 40,
            "max_states": 32,
        })
        states = build_topology_states(hsv, eligible, RANGES, config)
        deltas = {(s["delta_s"], s["delta_v"]) for s in states}
        self.assertIn((1, 0), deltas)
        self.assertIn((0, 1), deltas)
        self.assertNotIn((0, 0), deltas)

    def test_state_budget_reports_truncation(self):
        hsv = np.zeros((3, 3, 3), dtype=np.uint8)
        hsv[..., 1] = np.arange(140, 149).reshape(3, 3)
        hsv[..., 2] = np.arange(120, 129).reshape(3, 3)
        config, error = normalize_rescue_config({
            "mode": "shadow", "max_delta_s": 20, "max_delta_v": 20,
            "max_states": 2, "min_stable_states": 2,
        })
        self.assertIsNone(error)
        states = build_topology_states(
            hsv, np.ones((3, 3), dtype=bool), RANGES, config)
        self.assertTrue(states.truncated)
        self.assertGreater(states.total, len(states))


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
