"""Tests for audit_billing_changes.py.

The ``BillingAudit`` class is imported by ``generate_weekly_pdfs.py`` for
the production billing pipeline's price-anomaly / data-consistency
audit pass. Prior to this file the module sat at 8% coverage — only the
class definition was touched. These tests exercise every public method
plus the private helpers that don't require Smartsheet network calls
(``_detect_price_anomalies``, ``_validate_data_consistency``,
``_generate_audit_summary``, ``_compute_trend``,
``_selective_cell_history_enrichment``). The Smartsheet-touching
``_detect_suspicious_changes`` path is exercised with a mocked client
so the network is never hit.

Conventions: each test isolates audit state by patching
``audit_state_file`` to a temp path, so ``generated_docs/audit_state.json``
is never read or written during the suite.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import audit_billing_changes  # noqa: E402
from audit_billing_changes import BillingAudit  # noqa: E402


# --------------------------------------------------------------------------- #
# Test helpers
# --------------------------------------------------------------------------- #
def _make_audit(skip_cell_history: bool = True, tmpdir: str = "") -> BillingAudit:
    """Construct a BillingAudit with a stub client and a temp state file.

    Defaults to ``skip_cell_history=True`` because cell-history paths call
    the Smartsheet network. Tests that exercise those paths flip the flag
    and supply a mock client.
    """
    audit = BillingAudit(client=mock.MagicMock(), skip_cell_history=skip_cell_history)
    if tmpdir:
        audit.audit_state_file = os.path.join(tmpdir, "audit_state.json")
        # Reset in-memory state to the fresh defaults so the previous
        # constructor read of a real generated_docs/audit_state.json (if
        # any) doesn't leak into the test.
        audit.audit_state = {
            "last_audit_time": None,
            "monitored_sheets": {},
            "flagged_changes": [],
            "audit_summary": {},
        }
    return audit


def _row(**overrides) -> dict:
    """Build a valid-looking source row with sensible defaults."""
    base = {
        "Work Request #": "WR_100",
        "Units Total Price": "1000.00",
        "Quantity": "5",
        "CU": "ABC-123",
        "Foreman": "J. Doe",
        "Dept #": "500",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Initialization & state persistence
# --------------------------------------------------------------------------- #
class TestInit(unittest.TestCase):
    def test_init_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Construct directly (not via _make_audit) so we see the
            # production-default value of skip_cell_history.
            audit = BillingAudit(client=mock.MagicMock())
            audit.audit_state_file = os.path.join(tmp, "audit_state.json")
            audit.audit_state = audit._load_audit_state()
            self.assertFalse(audit.skip_cell_history)
            # Default audit state has the expected scaffold keys.
            self.assertIn("last_audit_time", audit.audit_state)
            self.assertIn("monitored_sheets", audit.audit_state)
            self.assertIn("flagged_changes", audit.audit_state)
            self.assertIn("audit_summary", audit.audit_state)

    def test_init_skip_cell_history_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(skip_cell_history=True, tmpdir=tmp)
            self.assertTrue(audit.skip_cell_history)

    def test_init_reads_audit_sheet_id_from_env(self):
        with mock.patch.dict(os.environ, {"AUDIT_SHEET_ID": "12345"}, clear=False):
            audit = BillingAudit(client=mock.MagicMock())
            self.assertEqual(audit.audit_sheet_id, "12345")


class TestLoadAuditState(unittest.TestCase):
    def test_load_returns_defaults_when_file_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            # File doesn't exist → defaults.
            state = audit._load_audit_state()
            self.assertEqual(state["last_audit_time"], None)
            self.assertEqual(state["audit_summary"], {})

    def test_load_returns_defaults_on_corrupt_file(self):
        # A corrupted JSON file must NOT crash the pipeline — the module's
        # production-safety contract is that audit is best-effort.
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            with open(audit.audit_state_file, "w", encoding="utf-8") as f:
                f.write("{ not valid json")
            state = audit._load_audit_state()
            self.assertIn("last_audit_time", state)
            self.assertIsNone(state["last_audit_time"])

    def test_load_reads_persisted_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            persisted = {
                "last_audit_time": "2026-04-24T12:00:00+00:00",
                "monitored_sheets": {},
                "flagged_changes": [],
                "audit_summary": {"risk_level": "MEDIUM", "total_anomalies": 2},
            }
            with open(audit.audit_state_file, "w", encoding="utf-8") as f:
                json.dump(persisted, f)
            state = audit._load_audit_state()
            self.assertEqual(state["audit_summary"]["risk_level"], "MEDIUM")
            self.assertEqual(state["audit_summary"]["total_anomalies"], 2)


class TestSaveAuditState(unittest.TestCase):
    def test_save_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            audit.audit_state["last_audit_time"] = "2026-04-24T12:00:00+00:00"
            audit.audit_state["audit_summary"] = {"risk_level": "HIGH"}
            audit._save_audit_state()

            with open(audit.audit_state_file, "r", encoding="utf-8") as f:
                disk = json.load(f)
            self.assertEqual(
                disk["last_audit_time"], "2026-04-24T12:00:00+00:00"
            )
            self.assertEqual(disk["audit_summary"]["risk_level"], "HIGH")

    def test_save_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            audit.audit_state_file = os.path.join(
                tmp, "nested", "deeper", "audit_state.json"
            )
            audit._save_audit_state()
            self.assertTrue(os.path.exists(audit.audit_state_file))

    def test_save_handles_io_error_without_raising(self):
        # The production pipeline must not break if the audit state file
        # cannot be written (read-only volume, permission denied, etc.).
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                try:
                    audit._save_audit_state()
                except Exception as e:  # noqa: BLE001
                    self.fail(
                        f"_save_audit_state must swallow IO errors but raised: {e!r}"
                    )


# --------------------------------------------------------------------------- #
# Price anomaly detection
# --------------------------------------------------------------------------- #
class TestDetectPriceAnomalies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_empty_for_empty_rows(self):
        self.assertEqual(self.audit._detect_price_anomalies([]), [])

    def test_returns_empty_when_one_row_per_wr(self):
        # The variance check requires at least 2 prices per WR.
        rows = [_row(**{"Work Request #": "WR_1", "Units Total Price": "100"})]
        self.assertEqual(self.audit._detect_price_anomalies(rows), [])

    def test_flags_high_variance_within_a_wr(self):
        # range/avg = (1000-100)/((1000+100)/2) = 900/550 = 1.636 > 0.5
        rows = [
            _row(**{"Work Request #": "WR_1", "Units Total Price": "100"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "1000"}),
        ]
        out = self.audit._detect_price_anomalies(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "price_variance_anomaly")
        self.assertEqual(out[0]["work_request"], "WR_1")
        self.assertEqual(out[0]["severity"], "medium")
        self.assertGreater(out[0]["variance_percentage"], 50.0)

    def test_does_not_flag_low_variance(self):
        # range/avg = (110-100)/((110+100)/2) = 10/105 ≈ 0.095 < 0.5
        rows = [
            _row(**{"Work Request #": "WR_1", "Units Total Price": "100"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "110"}),
        ]
        self.assertEqual(self.audit._detect_price_anomalies(rows), [])

    def test_strips_dollar_signs_and_commas(self):
        # Prices in Smartsheet are sometimes pre-formatted strings.
        rows = [
            _row(**{"Work Request #": "WR_1", "Units Total Price": "$1,000.00"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "$5,000.00"}),
        ]
        out = self.audit._detect_price_anomalies(rows)
        self.assertEqual(len(out), 1)
        # avg = 3000, range = 4000, var = 4000/3000 ≈ 133%
        self.assertAlmostEqual(out[0]["average_price"], 3000.0, places=2)
        self.assertAlmostEqual(out[0]["price_range"], 4000.0, places=2)

    def test_skips_unparseable_prices(self):
        # An unparseable price for one row in a WR group must not crash;
        # the row is silently dropped and the other rows still evaluated.
        rows = [
            _row(**{"Work Request #": "WR_1", "Units Total Price": "100"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "not-a-number"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "1000"}),
        ]
        # Two parseable prices → variance check still applies.
        out = self.audit._detect_price_anomalies(rows)
        self.assertEqual(len(out), 1)

    def test_groups_per_wr_independently(self):
        rows = [
            # WR_1 → flat, no anomaly.
            _row(**{"Work Request #": "WR_1", "Units Total Price": "100"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "105"}),
            # WR_2 → huge variance, anomaly.
            _row(**{"Work Request #": "WR_2", "Units Total Price": "100"}),
            _row(**{"Work Request #": "WR_2", "Units Total Price": "5000"}),
        ]
        out = self.audit._detect_price_anomalies(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["work_request"], "WR_2")

    def test_returns_empty_when_avg_price_is_zero(self):
        # avg=0 short-circuits the variance check to avoid DivisionByZero.
        rows = [
            _row(**{"Work Request #": "WR_1", "Units Total Price": "0"}),
            _row(**{"Work Request #": "WR_1", "Units Total Price": "0"}),
        ]
        self.assertEqual(self.audit._detect_price_anomalies(rows), [])


# --------------------------------------------------------------------------- #
# Data consistency
# --------------------------------------------------------------------------- #
class TestValidateDataConsistency(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_clean_row_produces_no_issues(self):
        self.assertEqual(
            self.audit._validate_data_consistency([_row()]), []
        )

    def test_missing_required_field_is_flagged(self):
        # The required-fields list is: Work Request #, Units Total Price,
        # Quantity, CU. Drop one and ensure it's caught.
        bad = _row()
        bad["CU"] = ""
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Missing CU", out[0]["issues"])
        self.assertEqual(out[0]["severity"], "low")

    def test_multiple_missing_fields_promote_severity_to_medium(self):
        bad = _row()
        bad["CU"] = ""
        bad["Quantity"] = ""
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "medium")
        self.assertIn("Missing CU", out[0]["issues"])
        self.assertIn("Missing Quantity", out[0]["issues"])

    def test_negative_price_is_flagged(self):
        bad = _row(**{"Units Total Price": "-50.00"})
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Negative price detected", out[0]["issues"])

    def test_invalid_price_format_is_flagged(self):
        bad = _row(**{"Units Total Price": "abc"})
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Invalid price format", out[0]["issues"])

    def test_zero_quantity_is_flagged(self):
        # Quantity="0" is a non-empty (truthy) string so the missing-field
        # check passes; the float branch then sees 0.0 and flags it.
        bad = _row(**{"Quantity": "0"})
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Zero or negative quantity", out[0]["issues"])

    def test_negative_quantity_is_flagged(self):
        # Negative quantity DOES parse and IS truthy as a string, so the
        # missing check passes and the float branch fires.
        bad = _row(**{"Quantity": "-2"})
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Zero or negative quantity", out[0]["issues"])

    def test_invalid_quantity_format_is_flagged(self):
        bad = _row(**{"Quantity": "five"})
        out = self.audit._validate_data_consistency([bad])
        self.assertEqual(len(out), 1)
        self.assertIn("Invalid quantity format", out[0]["issues"])

    def test_row_index_and_wr_are_attached(self):
        # The caller uses row_index + work_request to triangulate which
        # source row needs operator attention. Both fields must be present.
        bad1 = _row(**{"Work Request #": "WR_A"})
        bad1["CU"] = ""
        bad2 = _row(**{"Work Request #": "WR_B"})
        bad2["CU"] = ""
        out = self.audit._validate_data_consistency([_row(), bad1, bad2])
        self.assertEqual(len(out), 2)
        # First bad row is at index 1 in input order.
        self.assertEqual(out[0]["row_index"], 1)
        self.assertEqual(out[0]["work_request"], "WR_A")
        self.assertEqual(out[1]["row_index"], 2)
        self.assertEqual(out[1]["work_request"], "WR_B")


# --------------------------------------------------------------------------- #
# Summary & risk-level escalation
# --------------------------------------------------------------------------- #
class TestGenerateAuditSummary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_zero_issues_yields_low_risk(self):
        s = self.audit._generate_audit_summary(
            {
                "anomalies_detected": [],
                "unauthorized_changes": [],
                "data_integrity_issues": [],
            }
        )
        self.assertEqual(s["risk_level"], "LOW")
        self.assertEqual(s["total_anomalies"], 0)
        self.assertIn("No issues detected. Continue monitoring.", s["recommendations"])

    def test_three_or_fewer_issues_yields_medium_risk(self):
        s = self.audit._generate_audit_summary(
            {
                "anomalies_detected": [{}, {}],
                "unauthorized_changes": [],
                "data_integrity_issues": [{}],
            }
        )
        self.assertEqual(s["risk_level"], "MEDIUM")
        # Anomaly-driven recommendation must be appended.
        self.assertTrue(
            any("price anomalies" in r for r in s["recommendations"]),
            f"Missing price-anomaly recommendation in {s['recommendations']!r}",
        )

    def test_more_than_three_issues_yields_high_risk(self):
        s = self.audit._generate_audit_summary(
            {
                "anomalies_detected": [{}, {}],
                "unauthorized_changes": [{}],
                "data_integrity_issues": [{}, {}],
            }
        )
        self.assertEqual(s["risk_level"], "HIGH")
        self.assertEqual(s["total_anomalies"], 2)
        self.assertEqual(s["total_unauthorized_changes"], 1)
        self.assertEqual(s["total_data_issues"], 2)

    def test_data_issues_only_recommendation(self):
        s = self.audit._generate_audit_summary(
            {
                "anomalies_detected": [],
                "unauthorized_changes": [],
                "data_integrity_issues": [{}],
            }
        )
        self.assertTrue(
            any("data consistency" in r for r in s["recommendations"]),
        )


# --------------------------------------------------------------------------- #
# Trend computation
# --------------------------------------------------------------------------- #
class TestComputeTrend(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_baseline_when_no_previous_summary(self):
        # No previous summary in state → baseline marker.
        self.audit.audit_state["audit_summary"] = {}
        trend = self.audit._compute_trend(
            {"risk_level": "LOW", "total_anomalies": 0,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_direction"], "baseline")
        self.assertEqual(trend["risk_level_delta"], 0)
        self.assertEqual(trend["issues_delta"], 0)
        self.assertEqual(trend["issues_delta_pct"], "0%")

    def test_worsening_when_risk_level_increases(self):
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "LOW", "total_anomalies": 0,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "HIGH", "total_anomalies": 5,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_direction"], "worsening")
        self.assertEqual(trend["risk_level_delta"], 2)  # LOW=1, HIGH=3
        self.assertEqual(trend["issues_delta"], 5)

    def test_improving_when_risk_level_decreases(self):
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "HIGH", "total_anomalies": 5,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "LOW", "total_anomalies": 0,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_direction"], "improving")
        self.assertEqual(trend["risk_level_delta"], -2)
        self.assertEqual(trend["issues_delta"], -5)

    def test_stable_when_level_and_issues_match(self):
        prev = {
            "risk_level": "MEDIUM", "total_anomalies": 1,
            "total_unauthorized_changes": 1, "total_data_issues": 1,
        }
        self.audit.audit_state["audit_summary"] = prev
        trend = self.audit._compute_trend(dict(prev))
        self.assertEqual(trend["risk_direction"], "stable")
        self.assertEqual(trend["risk_level_delta"], 0)
        self.assertEqual(trend["issues_delta"], 0)

    def test_level_tie_breaks_to_worsening_on_more_issues(self):
        # Same risk level but issues grew → "worsening".
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "MEDIUM", "total_anomalies": 1,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "MEDIUM", "total_anomalies": 3,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_direction"], "worsening")
        self.assertEqual(trend["risk_level_delta"], 0)
        self.assertEqual(trend["issues_delta"], 2)

    def test_level_tie_breaks_to_improving_on_fewer_issues(self):
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "MEDIUM", "total_anomalies": 3,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "MEDIUM", "total_anomalies": 1,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_direction"], "improving")

    def test_unknown_levels_get_value_zero(self):
        # Implementation maps unknown risk strings to 0, so two unknowns
        # produce delta 0 (stable when issues also tie).
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "UNKNOWN", "total_anomalies": 0,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "BANANA", "total_anomalies": 0,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["risk_level_delta"], 0)
        self.assertEqual(trend["risk_direction"], "stable")

    def test_issues_delta_pct_handles_zero_baseline(self):
        # prev_issues=0 must not DivisionByZero — the implementation returns
        # the literal "0%" sentinel in that case.
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "LOW", "total_anomalies": 0,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "HIGH", "total_anomalies": 10,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        self.assertEqual(trend["issues_delta_pct"], "0%")

    def test_issues_delta_pct_computed_when_baseline_nonzero(self):
        self.audit.audit_state["audit_summary"] = {
            "risk_level": "MEDIUM", "total_anomalies": 2,
            "total_unauthorized_changes": 0, "total_data_issues": 0,
        }
        trend = self.audit._compute_trend(
            {"risk_level": "MEDIUM", "total_anomalies": 3,
             "total_unauthorized_changes": 0, "total_data_issues": 0}
        )
        # delta=1, base=2 → +50.0%
        self.assertEqual(trend["issues_delta_pct"], "50.0%")


# --------------------------------------------------------------------------- #
# Selective cell-history enrichment
# --------------------------------------------------------------------------- #
class TestSelectiveCellHistoryEnrichment(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_returns_empty_when_no_affected_wrs(self):
        rows = [_row()]
        out = self.audit._selective_cell_history_enrichment(
            rows, {"anomalies_detected": [], "data_integrity_issues": []}
        )
        self.assertEqual(out, [])

    def test_enriches_only_rows_for_affected_wrs(self):
        rows = [
            {**_row(**{"Work Request #": "WR_A"}),
             "__sheet_id": 1, "__row_id": 10},
            {**_row(**{"Work Request #": "WR_B"}),
             "__sheet_id": 1, "__row_id": 11},
            # WR_C has no sheet/row metadata → must be skipped.
            _row(**{"Work Request #": "WR_C"}),
        ]
        audit_results = {
            "anomalies_detected": [{"work_request": "WR_A"}],
            "data_integrity_issues": [{"work_request": "WR_C"}],
        }
        out = self.audit._selective_cell_history_enrichment(rows, audit_results)
        # WR_A enriches (has metadata); WR_C has no metadata so it's skipped;
        # WR_B is not affected so it's also skipped.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["work_request"], "WR_A")
        self.assertEqual(out[0]["sheet_id"], 1)
        self.assertEqual(out[0]["row_id"], 10)
        self.assertTrue(out[0]["history_available"])

    def test_accepts_either_work_request_field_name(self):
        # The detection helpers in this module write ``work_request``; an
        # alternate "work_request_number" key is also accepted defensively.
        rows = [
            {**_row(**{"Work Request #": "WR_A"}),
             "__sheet_id": 1, "__row_id": 10},
        ]
        audit_results = {
            "anomalies_detected": [{"work_request_number": "WR_A"}],
            "data_integrity_issues": [],
        }
        out = self.audit._selective_cell_history_enrichment(rows, audit_results)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["work_request"], "WR_A")


# --------------------------------------------------------------------------- #
# Suspicious-change detection (mocked Smartsheet client)
# --------------------------------------------------------------------------- #
class TestDetectSuspiciousChanges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_short_circuits_when_skip_cell_history_is_true(self):
        # When skip_cell_history=True the client must not be called at all
        # — this is the performance opt-out path documented in the module
        # docstring.
        audit = _make_audit(skip_cell_history=True, tmpdir=self._tmp)
        out = audit._detect_suspicious_changes([{"id": 1, "name": "S"}])
        self.assertEqual(out, [])
        audit.client.Sheets.get_sheet.assert_not_called()

    def test_flags_recent_discussion(self):
        audit = _make_audit(skip_cell_history=False, tmpdir=self._tmp)
        now = datetime.datetime.now(datetime.timezone.utc)
        recent = now - datetime.timedelta(hours=1)
        discussion = mock.MagicMock()
        discussion.id = 999
        discussion.created_at = recent
        sheet_info = mock.MagicMock()
        sheet_info.discussions = [discussion]
        audit.client.Sheets.get_sheet.return_value = sheet_info

        out = audit._detect_suspicious_changes(
            [{"id": 1, "name": "Sheet One"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "recent_discussion")
        self.assertEqual(out[0]["sheet_id"], 1)
        self.assertEqual(out[0]["discussion_id"], 999)

    def test_ignores_old_discussion(self):
        audit = _make_audit(skip_cell_history=False, tmpdir=self._tmp)
        old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        discussion = mock.MagicMock()
        discussion.id = 999
        discussion.created_at = old
        sheet_info = mock.MagicMock()
        sheet_info.discussions = [discussion]
        audit.client.Sheets.get_sheet.return_value = sheet_info

        out = audit._detect_suspicious_changes([{"id": 1, "name": "Sheet"}])
        self.assertEqual(out, [])

    def test_skips_sheets_without_id(self):
        audit = _make_audit(skip_cell_history=False, tmpdir=self._tmp)
        out = audit._detect_suspicious_changes([{"name": "Sheet"}])
        self.assertEqual(out, [])
        audit.client.Sheets.get_sheet.assert_not_called()

    def test_swallows_per_sheet_errors(self):
        # A failure on one sheet must not poison the whole audit.
        audit = _make_audit(skip_cell_history=False, tmpdir=self._tmp)
        audit.client.Sheets.get_sheet.side_effect = RuntimeError("API down")
        out = audit._detect_suspicious_changes([{"id": 1, "name": "Sheet"}])
        self.assertEqual(out, [])


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
class TestGetAuditStatus(unittest.TestCase):
    def test_reports_disabled_audit_sheet_when_env_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            # Make sure AUDIT_SHEET_ID isn't set.
            os.environ.pop("AUDIT_SHEET_ID", None)
            audit = BillingAudit(client=mock.MagicMock())
            status = audit.get_audit_status()
            self.assertTrue(status["audit_enabled"])
            self.assertFalse(status["audit_sheet_configured"])

    def test_reports_configured_audit_sheet_when_env_set(self):
        with mock.patch.dict(os.environ, {"AUDIT_SHEET_ID": "99"}, clear=False):
            audit = BillingAudit(client=mock.MagicMock())
            self.assertTrue(audit.get_audit_status()["audit_sheet_configured"])

    def test_reports_last_risk_level_from_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            audit.audit_state["audit_summary"] = {"risk_level": "HIGH"}
            self.assertEqual(audit.get_audit_status()["last_risk_level"], "HIGH")

    def test_defaults_last_risk_level_to_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = _make_audit(tmpdir=tmp)
            self.assertEqual(audit.get_audit_status()["last_risk_level"], "UNKNOWN")


class TestAuditFinancialDataEndToEnd(unittest.TestCase):
    """End-to-end exercise of the public ``audit_financial_data`` entry."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(skip_cell_history=True, tmpdir=self._tmp)
        # risk_trend.json is written next to audit_state_file — redirect by
        # patching the literal path used in the module.
        self._cwd_patcher = mock.patch("os.path.join", wraps=os.path.join)
        # Redirect the "generated_docs" risk_trend.json into the temp dir
        # by patching os.makedirs and json.dump indirectly via cwd.
        self._orig_cwd = os.getcwd()
        os.chdir(self._tmp)
        os.makedirs("generated_docs", exist_ok=True)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_clean_run_yields_low_risk(self):
        result = self.audit.audit_financial_data(
            source_sheets=[],
            current_rows=[_row(), _row(**{"Work Request #": "WR_2"})],
        )
        self.assertEqual(result["summary"]["risk_level"], "LOW")
        self.assertEqual(result["sheets_audited"], 0)
        self.assertEqual(result["rows_audited"], 2)
        # Trend block is always attached (baseline on first run).
        self.assertEqual(result["trend"]["risk_direction"], "baseline")
        # Audit state was persisted to disk and re-readable.
        self.assertTrue(os.path.exists(self.audit.audit_state_file))

    def test_run_with_price_anomaly_promotes_risk(self):
        result = self.audit.audit_financial_data(
            source_sheets=[],
            current_rows=[
                _row(**{"Work Request #": "WR_X", "Units Total Price": "100"}),
                _row(**{"Work Request #": "WR_X", "Units Total Price": "5000"}),
            ],
        )
        self.assertEqual(result["summary"]["total_anomalies"], 1)
        # 1 anomaly → MEDIUM (≤3 issues threshold).
        self.assertEqual(result["summary"]["risk_level"], "MEDIUM")

    def test_two_runs_produce_trend(self):
        # First run establishes baseline.
        self.audit.audit_financial_data(source_sheets=[], current_rows=[_row()])
        # Second run with anomalies must report a worsening trend, since
        # we went from LOW (0 issues) to MEDIUM (1 anomaly).
        result = self.audit.audit_financial_data(
            source_sheets=[],
            current_rows=[
                _row(**{"Work Request #": "WR_X", "Units Total Price": "100"}),
                _row(**{"Work Request #": "WR_X", "Units Total Price": "5000"}),
            ],
        )
        self.assertEqual(result["trend"]["risk_direction"], "worsening")
        self.assertGreater(result["trend"]["risk_level_delta"], 0)

    def test_handles_internal_exception_without_raising(self):
        # If a helper raises unexpectedly, audit_financial_data must record
        # the error in the result but never propagate — the billing
        # pipeline must keep running even when audit breaks.
        with mock.patch.object(
            self.audit, "_detect_price_anomalies",
            side_effect=RuntimeError("boom"),
        ):
            result = self.audit.audit_financial_data(
                source_sheets=[], current_rows=[_row()]
            )
        self.assertIn("error", result)
        self.assertIn("boom", result["error"])

    def test_persists_risk_trend_history(self):
        # The local risk_trend.json rolling history (capped at 50 entries)
        # is written alongside audit_state.json. Verify a single entry
        # appears after one run.
        self.audit.audit_financial_data(source_sheets=[], current_rows=[_row()])
        hist_path = os.path.join("generated_docs", "risk_trend.json")
        self.assertTrue(os.path.exists(hist_path))
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["risk_level"], "LOW")

    def test_risk_trend_history_caps_at_50_entries(self):
        # Pre-populate with 55 fake entries and ensure the 56th run trims
        # the file back to the documented 50-entry rolling window.
        hist_path = os.path.join("generated_docs", "risk_trend.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(
                [{"timestamp": f"t{i}", "risk_level": "LOW",
                  "total_issues": 0, "trend": {}} for i in range(55)],
                f,
            )
        self.audit.audit_financial_data(source_sheets=[], current_rows=[_row()])
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
        self.assertEqual(len(hist), 50)


# --------------------------------------------------------------------------- #
# Logging branches
# --------------------------------------------------------------------------- #
class TestLogAuditResults(unittest.TestCase):
    """Exercise the risk-level dispatch branches of ``_log_audit_results``."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.audit = _make_audit(tmpdir=self._tmp)

    def tearDown(self):
        import shutil

        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_high_risk_logs_warning(self):
        with mock.patch.object(self.audit.logger, "warning") as warn:
            self.audit._log_audit_results(
                {"summary": {"risk_level": "HIGH", "total_anomalies": 5,
                             "total_unauthorized_changes": 0,
                             "total_data_issues": 0}}
            )
            self.assertTrue(warn.called)

    def test_medium_risk_logs_info_with_warning_emoji(self):
        with mock.patch.object(self.audit.logger, "info") as info:
            self.audit._log_audit_results(
                {"summary": {"risk_level": "MEDIUM", "total_anomalies": 1,
                             "total_unauthorized_changes": 0,
                             "total_data_issues": 0}}
            )
            self.assertTrue(info.called)
            # The MEDIUM branch uses the ⚠️ emoji prefix.
            first_arg = info.call_args_list[0][0][0]
            self.assertIn("⚠️", first_arg)

    def test_low_risk_logs_info_with_check_emoji(self):
        with mock.patch.object(self.audit.logger, "info") as info:
            self.audit._log_audit_results(
                {"summary": {"risk_level": "LOW", "total_anomalies": 0,
                             "total_unauthorized_changes": 0,
                             "total_data_issues": 0}}
            )
            self.assertTrue(info.called)
            first_arg = info.call_args_list[0][0][0]
            self.assertIn("✅", first_arg)

    def test_log_to_audit_sheet_invoked_when_configured(self):
        self.audit.audit_sheet_id = "12345"
        with mock.patch.object(self.audit, "_log_to_audit_sheet") as lsh:
            self.audit._log_audit_results(
                {"summary": {"risk_level": "LOW", "total_anomalies": 0,
                             "total_unauthorized_changes": 0,
                             "total_data_issues": 0}}
            )
            lsh.assert_called_once()

    def test_log_to_audit_sheet_skipped_when_unconfigured(self):
        self.audit.audit_sheet_id = None
        with mock.patch.object(self.audit, "_log_to_audit_sheet") as lsh:
            self.audit._log_audit_results(
                {"summary": {"risk_level": "LOW", "total_anomalies": 0,
                             "total_unauthorized_changes": 0,
                             "total_data_issues": 0}}
            )
            lsh.assert_not_called()


if __name__ == "__main__":  # pragma: no cover - convenience
    unittest.main()
