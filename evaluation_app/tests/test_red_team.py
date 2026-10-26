"""Red-team / adversarial tests for doctor_eval_app.

These tests intentionally try to break the app along the surfaces a real user or
a hostile input could hit: export integrity, save integrity, reset behavior,
account lifecycle, and the sample-comparison read model. They are characterization
+ regression tests: where current behavior is a deliberate safety contract we lock
it in; where a fix was applied (foreign-key enforcement) we prove the new guard.
"""

import contextlib
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app.models import Sample
from doctor_eval_app.storage import DoctorEvalStore
from doctor_eval_app.exporting import write_csv, write_jsonl


VALID_DEDUCTIONS = {
    "completeness": [],
    "correctness": {},
    "readability": [],
}


def _scores(c=4, k=4, r=4, o=4):
    return {
        "completeness": c,
        "correctness": k,
        "readability": r,
        "overall_quality": o,
    }


class RedTeamDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ---- Export -----------------------------------------------------------

    def test_export_empty_dataset_writes_valid_files_without_crash(self):
        """No evaluations at all must still produce readable export files."""
        rows = self.store.export_rows()
        self.assertEqual(rows, [])

        csv_path = Path(self.tmp.name) / "empty.csv"
        jsonl_path = Path(self.tmp.name) / "empty.jsonl"
        write_csv(rows, csv_path)
        write_jsonl(rows, jsonl_path)

        self.assertTrue(csv_path.exists())
        self.assertTrue(jsonl_path.exists())
        # JSONL of zero rows is an empty document, not malformed JSON.
        self.assertEqual(jsonl_path.read_text(encoding="utf-8"), "")

    def test_csv_formula_injection_neutralized_for_malicious_evaluator_id(self):
        """A reviewer-controlled id that looks like a spreadsheet formula must be
        quoted so a spreadsheet does not execute it on open.

        Accounts created through the app can no longer carry such an id (see
        test_account_creation_refuses_formula_like_id), but an id that reaches
        the database another way -- an older database, the import-side
        backfill -- must still be neutralised on export."""
        malicious_id = "=HYPERLINK(\"http://evil\")"
        self.store.create_user_if_missing(malicious_id, "pw-123456", "evaluator")
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        self.store.save_evaluation(
            evaluator_id=malicious_id,
            sample_id="sample_001",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="@SUM(1+1)",
            timer={},
            llm_judge_view={},
        )

        csv_path = Path(self.tmp.name) / "inject.csv"
        write_csv(self.store.export_rows(), csv_path)
        with csv_path.open(newline="", encoding="utf-8") as f:
            data_row = list(csv.DictReader(f))[0]

        self.assertTrue(data_row["evaluator_id"].startswith("'="))
        self.assertTrue(data_row["detailed_comment"].startswith("'@"))

    def test_jsonl_roundtrip_preserves_unicode_newlines_and_long_comment(self):
        self.store.import_samples([Sample("sample_001", "노트", "경과", "judge")])
        comment = "한국어 줄1\n줄2 \"인용\"\t탭 \\백슬래시 " + ("긴" * 5000)
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment=comment,
            timer={},
            llm_judge_view={},
        )

        jsonl_path = Path(self.tmp.name) / "out.jsonl"
        write_jsonl(self.store.export_rows(), jsonl_path)
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["detailed_comment"], comment)
        # Raw source text must never leak into exports.
        self.assertNotIn("discharge_note", parsed)

    def test_export_to_existing_directory_raises_so_ui_can_report(self):
        """If the export target is a directory, the writer must raise (the UI
        turns this into an 'Export failed' dialog) rather than silently no-op."""
        target_dir = Path(self.tmp.name) / "a_directory"
        target_dir.mkdir()
        with self.assertRaises(OSError):
            write_csv([], target_dir)
        with self.assertRaises(OSError):
            write_jsonl([], target_dir)

    # ---- Reset ------------------------------------------------------------

    def test_reset_is_idempotent_and_keeps_users_and_samples(self):
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="x",
            timer={},
            llm_judge_view={},
        )
        self.assertEqual(len(self.store.export_rows()), 1)

        self.store.reset_evaluations()
        self.store.reset_evaluations()  # double reset must not raise

        self.assertEqual(self.store.export_rows(), [])
        self.assertEqual(self.store.progress_summary()["completed_evaluations"], 0)
        # Users and samples survive a reset.
        self.assertIsNotNone(self.store.authenticate("admin", "admin"))
        self.assertIsNotNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertEqual(
            len(self.store.list_samples_for_evaluator("evaluator_a")), 1
        )

    # ---- Save integrity ---------------------------------------------------

    def test_save_for_unknown_sample_rejected_by_foreign_key(self):
        """Regression: saving an evaluation for a sample that does not exist must
        be rejected, otherwise an orphan completed row inflates progress while
        being invisible in the sample comparison view."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_evaluation(
                evaluator_id="evaluator_a",
                sample_id="ghost_sample",
                display_order=1,
                scores=_scores(),
                deductions=VALID_DEDUCTIONS,
                detailed_comment="should not persist",
                timer={},
                llm_judge_view={},
            )
        self.assertEqual(self.store.export_rows(), [])

    def test_resave_overwrites_and_cannot_create_duplicate_primary_key(self):
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        for completeness in (1, 5, 3):
            self.store.save_evaluation(
                evaluator_id="evaluator_a",
                sample_id="sample_001",
                display_order=1,
                scores=_scores(c=completeness),
                deductions=VALID_DEDUCTIONS,
                detailed_comment="rev",
                timer={},
                llm_judge_view={},
            )
        rows = self.store.export_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["completeness_score"], 3)

    # ---- Account lifecycle ------------------------------------------------

    def test_duplicate_account_id_is_refused(self):
        self.store.create_evaluator_account("dup_user", "pw-123456", "pw-123456", name="A")
        with self.assertRaises(ValueError):
            self.store.create_evaluator_account(
                "dup_user", "other-pass1", "other-pass1", name="A2"
            )
        # The original account keeps its password.
        self.assertIsNotNone(self.store.authenticate("dup_user", "pw-123456"))
        self.assertIsNone(self.store.authenticate("dup_user", "other-pass1"))

    def test_account_creation_validates_id_and_password(self):
        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("   ", "pw-123456", "pw-123456")
        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("u2_eval", "pw-123456", "different")
        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("u3_eval", "short", "short")

    def test_account_creation_refuses_formula_like_id(self):
        for user_id in ("=HYPERLINK(1)", "+cmd", "-1+1", "@SUM(1)", "a b", "../etc"):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    self.store.create_evaluator_account(
                        user_id, "pw-123456", "pw-123456"
                    )

    def test_delete_admin_refused_and_unknown_evaluator_refused(self):
        with self.assertRaises(ValueError):
            self.store.delete_evaluator_account("admin")
        with self.assertRaises(ValueError):
            self.store.delete_evaluator_account("does_not_exist")
        # Admin still usable afterwards.
        self.assertIsNotNone(self.store.authenticate("admin", "admin"))

    def test_delete_evaluator_cascades_to_their_evaluations(self):
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        self.store.create_evaluator_account("temp_eval", "pw-123456", "pw-123456")
        self.store.save_evaluation(
            evaluator_id="temp_eval",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="x",
            timer={},
            llm_judge_view={},
        )
        self.assertEqual(len(self.store.export_rows()), 1)

        self.store.delete_evaluator_account("temp_eval")

        self.assertEqual(self.store.export_rows(), [])
        self.assertIsNone(self.store.authenticate("temp_eval", "pw-123456"))

    def _set_status(self, user_id, status):
        """Simulate a row left over from the old self-signup flow."""
        with self.store.conn:
            self.store.conn.execute(
                "update users set status = ? where user_id = ?", (status, user_id)
            )

    def test_legacy_rejected_id_stays_taken_and_blocked(self):
        """A rejected row from an older database keeps its id: re-creating it
        fails, so the admin must delete the record first to free the id."""
        self.store.create_evaluator_account("rej_user", "pw-123456", "pw-123456")
        self._set_status("rej_user", "rejected")

        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("rej_user", "pw-123456", "pw-123456")
        self.assertEqual(self.store.login_block_reason("rej_user"), "rejected")
        self.assertIsNone(self.store.authenticate("rej_user", "pw-123456"))

    def test_legacy_pending_and_rejected_users_cannot_authenticate(self):
        self.store.create_evaluator_account("pend_user", "pw-123456", "pw-123456")
        self._set_status("pend_user", "pending")
        self.assertIsNone(self.store.authenticate("pend_user", "pw-123456"))
        self.assertEqual(self.store.login_block_reason("pend_user"), "pending")

    def test_pending_or_rejected_row_does_not_count_as_admin(self):
        """has_admin gates the unauthenticated first-run path, so only an
        approved admin may close it."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = DoctorEvalStore(Path(tmp.name) / "fresh.db")
        store.seed_accounts = ()
        store.initialize()
        self.addCleanup(store.close)
        store.create_initial_admin("lab_admin", "pw-123456", "pw-123456")
        with store.conn:
            store.conn.execute("update users set status = 'rejected'")

        self.assertFalse(store.has_admin())

    # ---- Sample comparison read model -------------------------------------

    def test_comparison_view_excludes_drafts_and_unapproved_reviewers(self):
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        # evaluator_a completed
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(c=5),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="done",
            timer={},
            llm_judge_view={},
            completed=True,
        )
        # evaluator_b draft only (must NOT leak scores/comment)
        self.store.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(c=2),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="secret draft",
            timer={},
            llm_judge_view={},
            completed=False,
        )
        # Unapproved reviewers must never become columns.
        self.store.create_evaluator_account("pend_eval", "pw-123456", "pw-123456")
        self.store.create_evaluator_account("rej_eval", "pw-123456", "pw-123456")
        self._set_status("pend_eval", "pending")
        self._set_status("rej_eval", "rejected")

        comparison = self.store.sample_review_comparison_details()
        self.assertEqual(len(comparison), 1)
        reviewers = {r["evaluator_id"]: r for r in comparison[0]["reviewers"]}

        self.assertEqual(set(reviewers), {"evaluator_a", "evaluator_b", "evaluator_c"})
        self.assertNotIn("pend_eval", reviewers)
        self.assertNotIn("rej_eval", reviewers)

        self.assertTrue(reviewers["evaluator_a"]["completed"])
        self.assertEqual(reviewers["evaluator_a"]["scores"]["completeness"], 5)

        self.assertFalse(reviewers["evaluator_b"]["completed"])
        self.assertIsNone(reviewers["evaluator_b"]["scores"])
        self.assertEqual(reviewers["evaluator_b"]["detailed_comment"], "")


@contextlib.contextmanager
def _driven_app(question_answer="yes"):
    """Patch Qt entrypoints so run_app builds windows without an event loop.

    Yields (captured_windows, messages) where messages records info/warning/
    critical dialog argument tuples. Dialog 'question' returns Yes or No based on
    question_answer.
    """
    from PySide6.QtWidgets import (
        QApplication,
        QFileDialog,
        QMainWindow,
        QMessageBox,
    )

    captured = []
    messages = {"info": [], "warning": [], "critical": []}
    saved = {
        "show": QMainWindow.show,
        "exec": QApplication.exec,
        "info": QMessageBox.information,
        "warning": QMessageBox.warning,
        "critical": QMessageBox.critical,
        "question": QMessageBox.question,
        "dialog": QFileDialog.getSaveFileName,
        "open_dialog": QFileDialog.getOpenFileNames,
    }

    def capture_show(window):
        captured.append(window)
        return saved["show"](window)

    answer = QMessageBox.Yes if question_answer == "yes" else QMessageBox.No

    QMainWindow.show = capture_show
    QApplication.exec = lambda self: 0
    QMessageBox.information = lambda *a, **k: messages["info"].append(a) or QMessageBox.Ok
    QMessageBox.warning = lambda *a, **k: messages["warning"].append(a) or QMessageBox.Ok
    QMessageBox.critical = lambda *a, **k: messages["critical"].append(a) or QMessageBox.Ok
    QMessageBox.question = lambda *a, **k: answer
    try:
        yield captured, messages
    finally:
        QMainWindow.show = saved["show"]
        QApplication.exec = saved["exec"]
        QMessageBox.information = saved["info"]
        QMessageBox.warning = saved["warning"]
        QMessageBox.critical = saved["critical"]
        QMessageBox.question = saved["question"]
        QFileDialog.getSaveFileName = saved["dialog"]
        QFileDialog.getOpenFileNames = saved["open_dialog"]


def _seed_store_with_one_completed_evaluation(store):
    store.import_samples([Sample("sample_001", "note", "course", "judge")])
    store.save_evaluation(
        evaluator_id="evaluator_a",
        sample_id="sample_001",
        display_order=1,
        scores=_scores(),
        deductions=VALID_DEDUCTIONS,
        detailed_comment="seed",
        timer={},
        llm_judge_view={},
    )


ADMIN_TEST_PASSWORD = "admin-rotated-pw"


def _login_admin(window, store):
    # The fixture admin password is flagged must_change_password, which makes
    # login open a blocking rotation dialog. Rotate first so these tests exercise
    # the admin screens rather than the credential policy (covered separately).
    store.set_password("admin", ADMIN_TEST_PASSWORD)
    window.login.account.setText("admin")
    window.login.password.setText(ADMIN_TEST_PASSWORD)
    window.login.try_login()
    return window.stack.currentWidget()


def _find_button(widget, text):
    from PySide6.QtWidgets import QPushButton

    return [b for b in widget.findChildren(QPushButton) if b.text() == text][0]


class RedTeamUiTest(unittest.TestCase):
    def _require_qt(self):
        try:
            import PySide6.QtWidgets  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

    def test_admin_reset_cancel_preserves_evaluations(self):
        self._require_qt()
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with _driven_app(question_answer="no") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    _seed_store_with_one_completed_evaluation(store)
                    self.assertEqual(run_app(store), 0)
                    admin = _login_admin(captured[-1], store)

                    _find_button(admin, "Reset").click()

                    # Cancelled: data intact, no "Reset complete" info shown.
                    self.assertEqual(len(store.export_rows()), 1)
                    self.assertEqual(len(messages["info"]), 0)
                finally:
                    store.close()

    def test_admin_reset_confirm_clears_evaluations_only(self):
        self._require_qt()
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with _driven_app(question_answer="yes") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    _seed_store_with_one_completed_evaluation(store)
                    self.assertEqual(run_app(store), 0)
                    admin = _login_admin(captured[-1], store)

                    _find_button(admin, "Reset").click()

                    self.assertEqual(store.export_rows(), [])
                    self.assertGreaterEqual(len(messages["info"]), 1)
                    # Users and samples survive.
                    self.assertIsNotNone(store.authenticate("evaluator_a", "pass1234a"))
                    self.assertEqual(
                        len(store.list_samples_for_evaluator("evaluator_a")), 1
                    )
                finally:
                    store.close()

    def test_export_cancelled_dialog_writes_nothing(self):
        self._require_qt()
        from PySide6.QtWidgets import QFileDialog
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with _driven_app(question_answer="yes") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    _seed_store_with_one_completed_evaluation(store)
                    # User cancels the save dialog -> empty path.
                    QFileDialog.getSaveFileName = staticmethod(
                        lambda *a, **k: ("", "")
                    )
                    self.assertEqual(run_app(store), 0)
                    admin = _login_admin(captured[-1], store)

                    _find_button(admin, "Export CSV").click()

                    # Nothing written, no success or failure dialog.
                    self.assertEqual(len(messages["info"]), 0)
                    self.assertEqual(len(messages["critical"]), 0)
                    exports = list(Path(tmp).rglob("*.csv"))
                    self.assertEqual(exports, [])
                finally:
                    store.close()

    def test_admin_export_csv_failure_is_reported_not_silent(self):
        self._require_qt()
        from PySide6.QtWidgets import QFileDialog
        import doctor_eval_app.ui as ui_module
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        original_write_csv = ui_module.write_csv
        with _driven_app(question_answer="yes") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                target = str(Path(tmp) / "out.csv")
                try:
                    _seed_store_with_one_completed_evaluation(store)
                    QFileDialog.getSaveFileName = staticmethod(
                        lambda *a, **k: (target, "csv")
                    )

                    def boom(rows, path):
                        raise OSError("disk full")

                    ui_module.write_csv = boom

                    self.assertEqual(run_app(store), 0)
                    admin = _login_admin(captured[-1], store)
                    _find_button(admin, "Export CSV").click()

                    self.assertEqual(len(messages["critical"]), 1)
                    self.assertEqual(len(messages["info"]), 0)
                    self.assertFalse(Path(target).exists())
                finally:
                    ui_module.write_csv = original_write_csv
                    store.close()


SHARED_SAMPLES = [
    Sample("sample_001", "note one", "course one", "judge one"),
    Sample("sample_002", "note two", "course two", "judge two"),
]


def _new_store_with_samples(tmp, name):
    store = DoctorEvalStore(Path(tmp) / name / "doctor_eval.db")
    store.initialize()
    store.import_samples(SHARED_SAMPLES)
    return store


def _read_jsonl(path):
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class AggregationImportTest(unittest.TestCase):
    """Per-evaluator export -> admin import is the supported way to combine three
    reviewers without a shared concurrent database."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_admin_aggregates_exported_results_into_comparison(self):
        tmp = self.tmp.name

        store_a = _new_store_with_samples(tmp, "eval_a")
        store_a.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(c=5),
            deductions={
                "completeness": ["missing_reason"],
                "correctness": {"false_information": 2},
                "readability": [],
            },
            detailed_comment="A says check source",
            timer={},
            llm_judge_view={},
            completed=True,
        )
        a_file = Path(tmp) / "a.jsonl"
        write_jsonl(store_a.export_rows("evaluator_a"), a_file)
        store_a.close()

        store_b = _new_store_with_samples(tmp, "eval_b")
        store_b.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(c=3),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="B looks fine",
            timer={},
            llm_judge_view={},
            completed=True,
        )
        b_file = Path(tmp) / "b.jsonl"
        write_jsonl(store_b.export_rows("evaluator_b"), b_file)
        store_b.close()

        admin = _new_store_with_samples(tmp, "admin")
        try:
            summary = admin.import_evaluations(_read_jsonl(a_file) + _read_jsonl(b_file))
            self.assertEqual(summary["imported"], 2)
            self.assertEqual(summary["skipped_missing_sample"], 0)

            comparison = admin.sample_review_comparison_details()
            sample1 = next(s for s in comparison if s["sample_id"] == "sample_001")
            reviewers = {r["evaluator_id"]: r for r in sample1["reviewers"]}

            self.assertEqual(
                set(reviewers), {"evaluator_a", "evaluator_b", "evaluator_c"}
            )
            self.assertTrue(reviewers["evaluator_a"]["completed"])
            self.assertEqual(reviewers["evaluator_a"]["scores"]["completeness"], 5)
            self.assertEqual(
                reviewers["evaluator_a"]["detailed_comment"], "A says check source"
            )
            self.assertEqual(
                reviewers["evaluator_a"]["deductions"]["completeness"], ["missing_reason"]
            )
            self.assertEqual(
                reviewers["evaluator_a"]["deductions"]["correctness"]["false_information"],
                2,
            )
            self.assertEqual(
                reviewers["evaluator_a"]["deductions"]["correctness"]["causal_error"], 0
            )
            self.assertTrue(reviewers["evaluator_b"]["completed"])
            self.assertEqual(reviewers["evaluator_b"]["scores"]["completeness"], 3)
            # evaluator_c never submitted anywhere.
            self.assertFalse(reviewers["evaluator_c"]["completed"])
            self.assertIsNone(reviewers["evaluator_c"]["scores"])
        finally:
            admin.close()

    def test_import_skips_records_for_samples_not_in_admin_dataset(self):
        tmp = self.tmp.name
        record = {
            "evaluator_id": "evaluator_a",
            "sample_id": "sample_999_not_here",
            "display_order": 1,
            "completed": True,
            "scores": {
                "completeness": 4,
                "correctness": 4,
                "readability": 4,
                "overall_quality": 4,
            },
            "deductions": {"completeness": [], "correctness": {}, "readability": []},
            "detailed_comment": "orphan",
        }
        admin = _new_store_with_samples(tmp, "admin")
        try:
            summary = admin.import_evaluations([record])
            self.assertEqual(summary["imported"], 0)
            self.assertEqual(summary["skipped_missing_sample"], 1)
            # Nothing was inserted (no FK violation, no orphan row).
            self.assertEqual(admin.export_rows(), [])
        finally:
            admin.close()

    def test_import_auto_registers_unknown_evaluator_as_column(self):
        tmp = self.tmp.name
        record = {
            "evaluator_id": "dr_custom",
            "sample_id": "sample_001",
            "display_order": 1,
            "completed": True,
            "scores": {
                "completeness": 2,
                "correctness": 2,
                "readability": 2,
                "overall_quality": 2,
            },
            "deductions": {"completeness": [], "correctness": {}, "readability": []},
            "detailed_comment": "custom reviewer",
        }
        admin = _new_store_with_samples(tmp, "admin")
        try:
            summary = admin.import_evaluations([record])
            self.assertEqual(summary["imported"], 1)
            self.assertIn("dr_custom", summary["created_evaluators"])

            comparison = admin.sample_review_comparison_details()
            sample1 = next(s for s in comparison if s["sample_id"] == "sample_001")
            reviewers = {r["evaluator_id"]: r for r in sample1["reviewers"]}
            self.assertIn("dr_custom", reviewers)
            self.assertTrue(reviewers["dr_custom"]["completed"])
            # Auto-created evaluator cannot be logged into (random password).
            self.assertIsNone(admin.authenticate("dr_custom", "demo"))
        finally:
            admin.close()

    def test_import_skips_evaluator_ids_the_app_would_refuse(self):
        """An import must not plant accounts the dashboard cannot create:
        malformed IDs, case variants of an existing account, or an admin ID."""

        def record(evaluator_id):
            return {
                "evaluator_id": evaluator_id,
                "sample_id": "sample_001",
                "display_order": 1,
                "completed": True,
                "scores": {
                    "completeness": 3,
                    "correctness": 3,
                    "readability": 3,
                    "overall_quality": 3,
                },
                "deductions": {"completeness": [], "correctness": {}, "readability": []},
                "detailed_comment": "",
            }

        admin = _new_store_with_samples(self.tmp.name, "admin")
        try:
            # A row left over from the old self-signup flow is not a valid target.
            admin.create_evaluator_account("pend_eval", "pw-123456", "pw-123456")
            with admin.conn:
                admin.conn.execute(
                    "update users set status = 'pending' where user_id = 'pend_eval'"
                )
            bad_ids = [
                "../../evil", "<b>x</b>", "kim\n", "EVALUATOR_A", "Admin", "admin", "pend_eval",
            ]
            summary = admin.import_evaluations(
                [record(i) for i in bad_ids] + [record("evaluator_a"), record("dr_new")]
            )

            self.assertEqual(summary["skipped_invalid_evaluator"], len(bad_ids))
            self.assertEqual(summary["imported"], 2)
            self.assertEqual(summary["created_evaluators"], ["dr_new"])
            evaluators = {
                row["user_id"]
                for row in admin.conn.execute("select user_id from users").fetchall()
            }
            for bad in bad_ids:
                if bad not in ("admin", "pend_eval"):
                    self.assertNotIn(bad, evaluators)
            stored = {
                row["evaluator_id"]
                for row in admin.conn.execute(
                    "select evaluator_id from evaluations"
                ).fetchall()
            }
            self.assertEqual(stored, {"evaluator_a", "dr_new"})
        finally:
            admin.close()

    def test_import_is_idempotent_upsert(self):
        tmp = self.tmp.name
        record = {
            "evaluator_id": "evaluator_a",
            "sample_id": "sample_001",
            "display_order": 1,
            "completed": True,
            "scores": {
                "completeness": 4,
                "correctness": 4,
                "readability": 4,
                "overall_quality": 4,
            },
            "deductions": {"completeness": [], "correctness": {}, "readability": []},
            "detailed_comment": "v1",
        }
        admin = _new_store_with_samples(tmp, "admin")
        try:
            admin.import_evaluations([record])
            updated = dict(record, detailed_comment="v2")
            admin.import_evaluations([updated])

            rows = admin.export_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["detailed_comment"], "v2")
        finally:
            admin.close()

    def test_import_rejects_malformed_records(self):
        tmp = self.tmp.name
        admin = _new_store_with_samples(tmp, "admin")
        try:
            with self.assertRaises(ValueError):
                admin.import_evaluations([{"evaluator_id": "x", "sample_id": "sample_001"}])
            with self.assertRaises(ValueError):
                admin.import_evaluations(["not a dict"])
            # A rejected import must not have partially written anything.
            self.assertEqual(admin.export_rows(), [])
        finally:
            admin.close()

    def test_import_rejects_non_boolean_flags_without_partial_write(self):
        tmp = self.tmp.name
        valid_record = {
            "evaluator_id": "evaluator_a",
            "sample_id": "sample_001",
            "display_order": 1,
            "completed": True,
            "scores": {
                "completeness": 4,
                "correctness": 4,
                "readability": 4,
                "overall_quality": 4,
            },
            "deductions": {"completeness": [], "correctness": {}, "readability": []},
            "detailed_comment": "valid but must not partially import",
        }
        bad_completed = dict(valid_record)
        bad_completed.update(evaluator_id="string_completed", completed="false")
        bad_llm = dict(valid_record)
        bad_llm.update(
            evaluator_id="string_llm",
            llm_judge_view={"opened": "false"},
        )
        admin = _new_store_with_samples(tmp, "admin")
        try:
            with self.assertRaises(ValueError):
                admin.import_evaluations([valid_record, bad_completed])
            with self.assertRaises(ValueError):
                admin.import_evaluations([valid_record, bad_llm])
            self.assertEqual(admin.export_rows(), [])
        finally:
            admin.close()

    def test_export_filter_returns_only_that_evaluator(self):
        tmp = self.tmp.name
        store = _new_store_with_samples(tmp, "shared")
        try:
            for evaluator in ("evaluator_a", "evaluator_b"):
                store.save_evaluation(
                    evaluator_id=evaluator,
                    sample_id="sample_001",
                    display_order=1,
                    scores=_scores(),
                    deductions=VALID_DEDUCTIONS,
                    detailed_comment=evaluator,
                    timer={},
                    llm_judge_view={},
                )
            only_a = store.export_rows("evaluator_a")
            self.assertEqual(len(only_a), 1)
            self.assertEqual(only_a[0]["evaluator_id"], "evaluator_a")
            self.assertEqual(len(store.export_rows()), 2)
        finally:
            store.close()

    def test_full_field_roundtrip_preserves_every_field_including_llm_judge(self):
        """Every persisted evaluation field must survive export -> import, with
        deliberately distinct non-default values so any dropped/zeroed field fails."""
        tmp = self.tmp.name
        scores = {"completeness": 2, "correctness": 3, "readability": 4, "overall_quality": 5}
        deductions = {
            "completeness": ["missing_reason", "course_not_documented"],
            "correctness": {"false_information": 7, "causal_error": 3},
            "readability": ["abbreviations", "grammar"],
        }
        comment = "리뷰 코멘트\n둘째 줄 \"인용\"\t탭"
        timer = {
            "started_at": "2026-06-15T09:00:00+09:00",
            "ended_at": "2026-06-15T09:42:13+09:00",
            "active_seconds": 1234,
            "paused_seconds": 56,
            "pause_count": 3,
            "reset_count": 2,
        }
        llm_judge_view = {
            "opened": True,
            "first_opened_at": "2026-06-15T09:05:00+09:00",
            "open_count": 9,
            "visible_seconds": 321,
        }

        store_eval = _new_store_with_samples(tmp, "eval_full")
        store_eval.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=17,
            scores=scores,
            deductions=deductions,
            detailed_comment=comment,
            timer=timer,
            llm_judge_view=llm_judge_view,
            completed=True,
        )
        results = Path(tmp) / "full.jsonl"
        write_jsonl(store_eval.export_rows("evaluator_a"), results)
        store_eval.close()

        admin = _new_store_with_samples(tmp, "admin_full")
        try:
            admin.import_evaluations(_read_jsonl(results))
            row = admin.get_evaluation("evaluator_a", "sample_001")

            self.assertIsNotNone(row)
            self.assertEqual(row["display_order"], 17)
            self.assertEqual(row["completed"], 1)
            self.assertEqual(row["scores"], scores)
            self.assertEqual(row["overall_quality"], 5)
            self.assertEqual(row["detailed_comment"], comment)

            stored_deductions = json.loads(row["deductions_json"])
            self.assertEqual(
                sorted(stored_deductions["completeness"]),
                ["course_not_documented", "missing_reason"],
            )
            self.assertEqual(
                stored_deductions["correctness"],
                {"false_information": 7, "causal_error": 3},
            )
            self.assertEqual(
                sorted(stored_deductions["readability"]), ["abbreviations", "grammar"]
            )

            # Timer fields
            self.assertEqual(row["started_at"], timer["started_at"])
            self.assertEqual(row["ended_at"], timer["ended_at"])
            self.assertEqual(row["active_seconds"], 1234)
            self.assertEqual(row["paused_seconds"], 56)
            self.assertEqual(row["pause_count"], 3)
            self.assertEqual(row["reset_count"], 2)

            # LLM-as-Judge tracking fields
            self.assertEqual(row["llm_judge_opened"], 1)
            self.assertEqual(row["llm_judge_first_opened_at"], "2026-06-15T09:05:00+09:00")
            self.assertEqual(row["llm_judge_open_count"], 9)
            self.assertEqual(row["llm_judge_visible_seconds"], 321)
        finally:
            admin.close()


class AggregationUiTest(unittest.TestCase):
    def _require_qt(self):
        try:
            import PySide6.QtWidgets  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

    def test_evaluator_export_button_writes_own_results(self):
        self._require_qt()
        from PySide6.QtWidgets import QFileDialog
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with _driven_app(question_answer="yes") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    _seed_store_with_one_completed_evaluation(store)
                    target = str(Path(tmp) / "evaluator_a_results.jsonl")
                    QFileDialog.getSaveFileName = staticmethod(
                        lambda *a, **k: (target, "JSONL (*.jsonl)")
                    )
                    self.assertEqual(run_app(store), 0)
                    window = captured[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    _find_button(evaluator, "Export My Results").click()

                    self.assertTrue(Path(target).exists())
                    records = _read_jsonl(target)
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]["evaluator_id"], "evaluator_a")
                    self.assertEqual(len(messages["info"]), 1)
                finally:
                    store.close()

    def test_admin_import_button_merges_file_into_comparison(self):
        self._require_qt()
        from PySide6.QtWidgets import QFileDialog
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with tempfile.TemporaryDirectory() as tmp:
            # Build one evaluator's exported result file.
            eval_store = DoctorEvalStore(Path(tmp) / "eval_src" / "doctor_eval.db")
            eval_store.initialize()
            eval_store.import_samples([Sample("sample_001", "note", "course", "judge")])
            eval_store.save_evaluation(
                evaluator_id="evaluator_b",
                sample_id="sample_001",
                display_order=1,
                scores=_scores(c=5),
                deductions=VALID_DEDUCTIONS,
                detailed_comment="imported from B",
                timer={},
                llm_judge_view={},
                completed=True,
            )
            results_file = str(Path(tmp) / "evaluator_b_results.jsonl")
            write_jsonl(eval_store.export_rows("evaluator_b"), results_file)
            eval_store.close()

            with _driven_app(question_answer="yes") as (captured, messages):
                store = create_store(Path(tmp) / "admin_app")
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])
                    QFileDialog.getOpenFileNames = staticmethod(
                        lambda *a, **k: ([results_file], "JSONL (*.jsonl)")
                    )
                    self.assertEqual(run_app(store), 0)
                    admin = _login_admin(captured[-1], store)

                    _find_button(admin, "Import Results").click()

                    self.assertGreaterEqual(len(messages["info"]), 1)
                    comparison = store.sample_review_comparison_details()
                    reviewers = {
                        r["evaluator_id"]: r for r in comparison[0]["reviewers"]
                    }
                    self.assertTrue(reviewers["evaluator_b"]["completed"])
                    self.assertEqual(
                        reviewers["evaluator_b"]["detailed_comment"], "imported from B"
                    )
                finally:
                    store.close()


class LlmJudgeCountTest(unittest.TestCase):
    """The evaluation screen no longer exposes an LLM-as-Judge panel, so its
    exposure counters must stay at zero for the whole session. The sample text
    and the stored columns are untouched; only the evaluator's view of them is."""

    def _require_qt(self):
        try:
            import PySide6.QtWidgets  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

    def test_judge_panel_is_absent_and_counters_stay_zero(self):
        self._require_qt()
        from doctor_eval_app.app import create_store
        from doctor_eval_app.ui import run_app

        with _driven_app(question_answer="yes") as (captured, messages):
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge body"),
                            Sample("sample_002", "note 2", "course 2", "judge body 2"),
                        ]
                    )
                    self.assertEqual(run_app(store), 0)
                    window = captured[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    ev = window.stack.currentWidget()

                    self.assertFalse(hasattr(ev, "judge"))
                    self.assertFalse(hasattr(ev, "toggle_judge"))
                    self.assertEqual(ev.llm_open_count, 0)
                    self.assertIsNone(ev.llm_first_opened_at)
                    self.assertIsNone(ev.llm_last_opened)

                    # Still zero after moving between samples.
                    ev.next_sample()
                    self.assertEqual(ev.llm_open_count, 0)
                    self.assertEqual(ev.llm_visible_seconds, 0)
                    self.assertIsNone(ev.llm_first_opened_at)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
