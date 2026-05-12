"""Tests for cleanup_excels.py.

cleanup_excels.py is the standalone cleanup utility used by the GitHub
Actions ``cleanup_only`` mode. It removes stale ``WR_*.xlsx`` files
under ``generated_docs/`` while preserving the most recent variant per
``(WR, WeekEnding)`` identity. Prior to this file the module had 0%
coverage; these tests exercise the pure helpers and the side-effecting
``cleanup()`` entrypoint against a temp directory so production data
is never touched.
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import List
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cleanup_excels  # noqa: E402


class TestIdentify(unittest.TestCase):
    """Cover the ``identify()`` filename parser."""

    def test_returns_wr_and_week_for_canonical_name(self):
        # Canonical production filename shape.
        result = cleanup_excels.identify(
            "WR_12345_WeekEnding_081725_20260424_120000_abcdef12.xlsx"
        )
        self.assertEqual(result, ("12345", "081725"))

    def test_returns_none_when_prefix_missing(self):
        self.assertIsNone(
            cleanup_excels.identify("Report_12345_WeekEnding_081725.xlsx")
        )

    def test_returns_none_when_weekending_token_missing(self):
        self.assertIsNone(cleanup_excels.identify("WR_12345_Report_081725.xlsx"))

    def test_returns_none_when_too_few_parts(self):
        # ``WR_12345.xlsx`` splits into only 2 parts; parts[3] would IndexError.
        # The function must catch that and return None, not raise.
        self.assertIsNone(cleanup_excels.identify("WR_12345.xlsx"))

    def test_handles_helper_variant_filename(self):
        # Helper variants embed ``_Helper_<name>`` after the week; identify()
        # only cares about positions 1 and 3 so it should still parse cleanly.
        result = cleanup_excels.identify(
            "WR_99999_WeekEnding_120125_20260424_120000_deadbeef_Helper_Jdoe.xlsx"
        )
        self.assertEqual(result, ("99999", "120125"))

    def test_handles_vaccrew_variant_filename(self):
        result = cleanup_excels.identify(
            "WR_42_WeekEnding_010126_20260424_120000_cafebabe_VacCrew.xlsx"
        )
        self.assertEqual(result, ("42", "010126"))

    def test_non_xlsx_extension_still_parses(self):
        # identify() doesn't enforce extension — that's the caller's job.
        # Asserts the parser is loose on extension but tight on shape.
        result = cleanup_excels.identify("WR_1_WeekEnding_010126_foo.txt")
        self.assertEqual(result, ("1", "010126"))


class TestFindLatest(unittest.TestCase):
    """Cover ``find_latest()`` lexical-newest-per-identity logic."""

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(cleanup_excels.find_latest([]), {})

    def test_skips_unparseable_filenames(self):
        # Filenames identify() rejects must NOT appear in the result.
        out = cleanup_excels.find_latest(
            ["not_a_wr_file.xlsx", "WR_1.xlsx", "README.md"]
        )
        self.assertEqual(out, {})

    def test_picks_lexically_latest_per_identity(self):
        # Two timestamps for the same (WR, week); the lexically-later one wins
        # because the production filename ordering is timestamp-then-hash.
        older = "WR_100_WeekEnding_081725_20260101_120000_aaaaaaaa.xlsx"
        newer = "WR_100_WeekEnding_081725_20260424_120000_bbbbbbbb.xlsx"
        out = cleanup_excels.find_latest([newer, older])
        self.assertEqual(out, {("100", "081725"): newer})

    def test_picks_lexically_latest_regardless_of_input_order(self):
        older = "WR_100_WeekEnding_081725_20260101_120000_aaaaaaaa.xlsx"
        newer = "WR_100_WeekEnding_081725_20260424_120000_bbbbbbbb.xlsx"
        # Inserted older-first to prove order-independence.
        out = cleanup_excels.find_latest([older, newer])
        self.assertEqual(out, {("100", "081725"): newer})

    def test_keeps_one_per_distinct_identity(self):
        files = [
            "WR_100_WeekEnding_081725_20260101_120000_aaaaaaaa.xlsx",
            "WR_100_WeekEnding_082425_20260101_120000_bbbbbbbb.xlsx",  # diff week
            "WR_200_WeekEnding_081725_20260101_120000_cccccccc.xlsx",  # diff wr
        ]
        out = cleanup_excels.find_latest(files)
        self.assertEqual(len(out), 3)
        self.assertIn(("100", "081725"), out)
        self.assertIn(("100", "082425"), out)
        self.assertIn(("200", "081725"), out)

    def test_different_variants_share_identity_and_collide(self):
        # identify() keys on (WR, week) only — primary, helper, and vac_crew
        # variants for the same (WR, week) share an identity. find_latest()
        # therefore keeps the lexically-latest filename across variants.
        # This documents (rather than tests against) the production reality
        # that variant filenames embed a sortable timestamp + hash so all
        # three variants survive only when their timestamps differ.
        primary = "WR_5_WeekEnding_081725_20260101_120000_aaaaaaaa.xlsx"
        helper = (
            "WR_5_WeekEnding_081725_20260101_120000_bbbbbbbb_Helper_X.xlsx"
        )
        out = cleanup_excels.find_latest([primary, helper])
        self.assertEqual(len(out), 1)
        # ``b`` > ``a`` lexically, so the helper variant wins.
        self.assertEqual(out[("5", "081725")], helper)


class TestCleanup(unittest.TestCase):
    """Cover ``cleanup()`` against a temp ``OUTPUT_DIR``."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="cleanup_excels_test_")
        self._patcher = mock.patch.object(cleanup_excels, "OUTPUT_DIR", self._tmp)
        self._patcher.start()
        # Silence the script's print() noise during tests.
        self._print_patcher = mock.patch("builtins.print")
        self._print_patcher.start()

    def tearDown(self):
        self._print_patcher.stop()
        self._patcher.stop()
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def _touch(self, name: str) -> str:
        path = os.path.join(self._tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        return path

    def _list(self) -> List[str]:
        return sorted(os.listdir(self._tmp))

    def test_no_output_directory_is_a_no_op(self):
        # cleanup() must NOT raise when the directory is missing; that's the
        # documented "safe if directory absent" contract from the module
        # docstring. Important because GitHub Actions cleanup_only mode may
        # run on a fresh runner before any Excel has been generated.
        import shutil

        shutil.rmtree(self._tmp)
        try:
            cleanup_excels.cleanup()
        except Exception as e:  # noqa: BLE001
            self.fail(f"cleanup() raised on missing directory: {e!r}")

    def test_empty_directory_is_a_no_op(self):
        cleanup_excels.cleanup()
        self.assertEqual(self._list(), [])

    def test_keeps_only_latest_per_identity(self):
        older = "WR_100_WeekEnding_081725_20260101_120000_aaaaaaaa.xlsx"
        newer = "WR_100_WeekEnding_081725_20260424_120000_bbbbbbbb.xlsx"
        other_week = "WR_100_WeekEnding_082425_20260101_120000_cccccccc.xlsx"
        for n in (older, newer, other_week):
            self._touch(n)

        cleanup_excels.cleanup()

        remaining = self._list()
        self.assertIn(newer, remaining)
        self.assertIn(other_week, remaining)
        self.assertNotIn(older, remaining)

    def test_preserves_non_wr_files(self):
        # cleanup() must not touch files that don't match its WR pattern;
        # this is important because the same directory holds discovery_cache
        # / audit_state / hash_history JSON files alongside Excels.
        self._touch("WR_1_WeekEnding_010126_20260101_120000_aaaaaaaa.xlsx")
        self._touch("discovery_cache.json")
        self._touch("hash_history.json")
        # Unrelated xlsx that doesn't match the WR prefix should also survive.
        self._touch("manual_report.xlsx")

        cleanup_excels.cleanup()

        remaining = self._list()
        self.assertIn("discovery_cache.json", remaining)
        self.assertIn("hash_history.json", remaining)
        self.assertIn("manual_report.xlsx", remaining)

    def test_ignores_non_xlsx_wr_files(self):
        # cleanup() only enumerates ``.xlsx`` files matching the WR prefix.
        # A stray ``WR_*.csv`` for example must not be touched.
        self._touch("WR_1_WeekEnding_010126_legacy.csv")
        self._touch("WR_1_WeekEnding_010126_20260101_120000_aaaaaaaa.xlsx")

        cleanup_excels.cleanup()

        self.assertIn("WR_1_WeekEnding_010126_legacy.csv", self._list())


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
