import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path

from app import VersionedVault


class StableWriteTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.root = self.dir / "vault"
        self.log = self.root / "versions.jsonl"
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    # --- conflicting / unstable values are rejected before any side effect ---

    def test_conflicting_keys_rejected_without_directory(self):
        cases = [
            {1: "a", "1": "b"},
            {True: 1, "true": 2},
            {None: 1, "null": 2},
            {1.0: "a", "1.0": "b"},
            {float("nan"): 1, float("nan"): 2},
            {"a": 1, 1: 2},                      # digest cannot be computed (sort_keys)
            {"nested": {1: "a", "1": "b"}},      # collision nested inside value
            {"nested": {True: 1, "true": 2}},
            {"nested": {float("nan"): 1, float("nan"): 2}},
            {1, 2},                               # not JSON serializable
        ]
        for bad in cases:
            root = self.dir / ("v-" + str(len(list(self.dir.iterdir()))))
            vault = VersionedVault(root)
            with self.assertRaises(TypeError):
                vault.put("bad", bad)
            self.assertFalse(root.exists(), "rejected put must not create %s" % root)

    def test_circular_reference_rejected_without_directory(self):
        root = self.dir / "circ"
        vault = VersionedVault(root)
        bad = {}
        bad["self"] = bad
        with self.assertRaises(TypeError):
            vault.put("loop", bad)
        self.assertFalse(root.exists())

    def test_failure_then_retry_keeps_versions_continuous(self):
        vault = VersionedVault(self.root)
        self.assertEqual(vault.put("mode", "safe"), 1)
        with self.assertRaises(TypeError):
            vault.put("mode", {1: "a", "1": "b"})
        # state, active version and next version number all unchanged
        self.assertEqual(vault.active_version("mode"), 1)
        self.assertEqual(vault.get("mode"), "safe")
        self.assertEqual(vault.put("mode", "strict"), 2)
        self.assertEqual(vault.get("mode"), "strict")
        self.assertEqual(vault.active_version("mode"), 2)
        with self.log.open(encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            item = json.loads(line)
            self.assertEqual(item["digest"], VersionedVault._digest(item))
        versions = [json.loads(l)["version"] for l in lines]
        self.assertEqual(versions, [1, 2])

    # --- stable values produce one valid record and consistent reads ---

    def test_stable_values_round_trip_in_instance(self):
        vault = VersionedVault(self.root)
        values = [
            "plain string",
            42,
            3.5,
            True,
            None,
            [1, "two", [3, {"four": 4}]],
            {"k": "v", "n": {"a": [True, None]}},
            {"1": "lone int-keyed name survives"},
            (1, 2),  # persisted as JSON array
        ]
        for i, value in enumerate(values, start=1):
            self.assertEqual(vault.put("item", value), i)
        self.assertEqual(vault.active_version("item"), len(values))
        # tuple reads back as a list, everything else identical
        expected = [json.loads(json.dumps(v)) for v in values]
        history = [h["value"] for h in vault.history("item")]
        self.assertEqual(history, expected)
        for i, value in enumerate(expected, start=1):
            self.assertEqual(vault.get("item", i), value)
        self.assertEqual(vault.get("item"), expected[-1])

    def test_put_appends_single_valid_record(self):
        vault = VersionedVault(self.root)
        vault.put("a", 1)
        vault.put("b", {"x": [1, 2]})
        vault.put("a", 2)
        with self.log.open(encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)
        prev = 0
        for line in lines:
            item = json.loads(line)
            self.assertEqual(set(item), {"version", "name", "value", "digest"})
            self.assertEqual(item["version"], prev + 1)
            self.assertEqual(item["digest"], VersionedVault._digest(item))
            prev = item["version"]

    def test_cross_instance_reload_matches(self):
        vault = VersionedVault(self.root)
        vault.put("mode", "safe")
        vault.put("region", "east")
        vault.put("mode", ["strict", {"n": 1}])
        other = VersionedVault(self.root)
        self.assertEqual(other.versions(), vault.versions())
        self.assertEqual(other.history(), vault.history())
        for name in ("mode", "region"):
            self.assertEqual(other.get(name), vault.get(name))
            self.assertEqual(other.active_version(name), vault.active_version(name))
        vault.reload()
        third = VersionedVault(self.root)
        self.assertEqual(third.versions(), vault.versions())

    def test_deep_copy_isolation_preserved(self):
        vault = VersionedVault(self.root)
        vault.put("cfg", {"nested": [1, 2, 3]})
        got = vault.get("cfg")
        got["nested"].append(99)
        hist = vault.history("cfg")[0]["value"]
        hist["nested"].append(88)
        self.assertEqual(vault.get("cfg"), {"nested": [1, 2, 3]})

    # --- corruption remains ValueError and never mutates state or log ---

    def _corrupt(self, vault):
        vault.put("mode", "safe")
        before = self.log.read_bytes()
        with self.log.open("ab") as f:
            f.write(b"{not json\n")
        return before

    def test_reload_on_corrupt_log_keeps_state(self):
        vault = VersionedVault(self.root)
        self._corrupt(vault)
        snapshot = vault.versions()
        with self.assertRaises(ValueError):
            vault.reload()
        self.assertEqual(vault.versions(), snapshot)
        self.assertEqual(vault.get("mode"), "safe")
        self.assertEqual(vault.active_version("mode"), 1)

    def test_put_on_corrupt_log_changes_nothing(self):
        vault = VersionedVault(self.root)
        self._corrupt(vault)
        corrupt_bytes = self.log.read_bytes()
        snapshot = vault.versions()
        with self.assertRaises(ValueError):
            vault.put("mode", "strict")
        # in-memory state, active version, log bytes all untouched
        self.assertEqual(vault.versions(), snapshot)
        self.assertEqual(vault.get("mode"), "safe")
        self.assertEqual(self.log.read_bytes(), corrupt_bytes)
        # still the same failure: the bad chain was not appended past
        with self.assertRaises(ValueError):
            vault.put("other", "x")
        self.assertEqual(self.log.read_bytes(), corrupt_bytes)

    def test_new_instance_on_corrupt_log_raises(self):
        vault = VersionedVault(self.root)
        self._corrupt(vault)
        with self.assertRaises(ValueError):
            VersionedVault(self.root)

    # --- existing API contracts unchanged ---

    def test_name_validation_still_value_error(self):
        vault = VersionedVault(self.root)
        for bad in ("", 1, None):
            with self.assertRaises(ValueError):
                vault.put(bad, 1)

    def test_history_order_and_name_filter(self):
        vault = VersionedVault(self.root)
        vault.put("a", 1)
        vault.put("b", 9)
        vault.put("a", 2)
        self.assertEqual([h["version"] for h in vault.history()], [1, 2, 3])
        self.assertEqual([h["value"] for h in vault.history("a")], [1, 2])
        self.assertEqual([v["name"] for v in vault.versions()], ["a", "b"])
        with self.assertRaises(KeyError):
            vault.get("a", 9)


if __name__ == "__main__":
    unittest.main()
