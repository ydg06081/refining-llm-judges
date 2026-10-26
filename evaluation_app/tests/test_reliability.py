"""Tests for the data-loss / field-reliability fixes.

Each test pins a specific failure mode a clinician could hit while running the
app unsupervised, so the fix cannot silently regress. The scarce resource is the
clinician's completed work; these guard it.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app.app import StartupError, create_store, ensure_sample_data
from doctor_eval_app.models import Sample
from doctor_eval_app.sample_data import REAL_SAMPLE_CSV_NAME
from doctor_eval_app.storage import DoctorEvalStore, _backup_stamp
from doctor_eval_app.ui import _atomic_write_text, _coerce_int


THREE_COL_CSV = (
    "sample_id,discharge_note_no_bhc,brief_hospital_course\n"
    "1001,note one,course one\n"
    "1002,note two,course two\n"
)


def _write_dataset(base_dir):
    (base_dir / "data").mkdir(parents=True, exist_ok=True)
    (base_dir / "data" / REAL_SAMPLE_CSV_NAME).write_text(THREE_COL_CSV, encoding="utf-8")


class BackupPruneTest(unittest.TestCase):
    """The newest snapshot must survive pruning even though shutil.copy2 stamps
    backups with the SOURCE db's mtime, which can be older than an earlier
    backup's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DoctorEvalStore(Path(self.tmp.name) / "storage" / "doctor_eval.db")
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_stamp_parser_extracts_the_trailing_timestamp(self):
        self.assertEqual(
            _backup_stamp("doctor_eval.auto-reset_evaluations-20260722-224438-494830.db"),
            "20260722-224438-494830",
        )
        # Reason text containing digits and underscores must not confuse it.
        self.assertEqual(
            _backup_stamp("doctor_eval.auto-delete_evaluation_evaluator_a_demo_01_A-20260101-000000-000001.db"),
            "20260101-000000-000001",
        )
        self.assertIsNone(_backup_stamp("hand-named-backup.db"))

    def test_prune_keeps_the_backup_taken_last_not_the_one_with_newest_mtime(self):
        db = self.store.db_path
        # Backup A: taken first, while the db has a fresh mtime.
        os.utime(db, (time.time(), time.time()))
        a = self.store.backup_database("A_taken_first")
        # Backup B: taken later, but the db has not been written for "days", so
        # copy2 gives B an OLDER mtime than A.
        old = time.time() - 12 * 86400
        os.utime(db, (old, old))
        b = self.store.backup_database("B_taken_last")

        backups_dir = db.parent / "backups"
        self.store._prune_auto_backups(backups_dir, keep=1)
        survivors = [p.name for p in backups_dir.glob("*.auto-*.db")]
        self.assertEqual(survivors, [b.name], "pruned the most recent backup")


class FullResetTest(unittest.TestCase):
    """`전체 초기화` returns the DB to a clean, default state for a new round."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DoctorEvalStore(Path(self.tmp.name) / "storage" / "doctor_eval.db")
        self.store.initialize()
        self.store.import_samples([Sample("s1", "note", "course", "judge")])
        from doctor_eval_app.models import User

        self.admin = User(user_id="admin", role="admin")
        self.store.set_actor(self.admin)
        # A finished round: an extra account, a rotated password, evaluations.
        self.store.create_user_if_missing("evaluator_t", "pw_t", "evaluator", name="T")
        self.store.set_password("evaluator_a", "rotated-pw-a")
        for ev in ("evaluator_a", "evaluator_b", "evaluator_t"):
            self.store.save_evaluation(
                evaluator_id=ev, sample_id="s1", display_order=1,
                scores={"completeness": 4, "correctness": 4, "readability": 4,
                        "overall_quality": 4},
                deductions={"completeness": [], "correctness": {}, "readability": []},
                detailed_comment="x", timer={}, llm_judge_view={},
            )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _evaluators(self):
        return sorted(
            r["user_id"]
            for r in self.store.conn.execute(
                "select user_id from users where role = 'evaluator'"
            )
        )

    def test_reset_all_clears_evaluations_and_evaluator_accounts(self):
        summary = self.store.reset_all()
        self.assertEqual(summary["evaluations_removed"], 3)
        self.assertEqual(summary["evaluator_accounts_removed"], 4)

        self.assertEqual(
            self.store.conn.execute("select count(*) from evaluations").fetchone()[0], 0
        )
        # Every evaluator is gone and nothing is re-seeded; the admin creates
        # the new round's accounts from the dashboard.
        self.assertEqual(self._evaluators(), [])
        self.assertIsNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertIsNotNone(self.store.authenticate("admin", "admin"))
        self.store.create_evaluator_account("next_eval", "pw-123456", "pw-123456")
        self.assertEqual(self._evaluators(), ["next_eval"])
        # Samples and the admin account are kept.
        self.assertEqual(
            self.store.conn.execute("select count(*) from samples").fetchone()[0], 1
        )
        self.assertIsNotNone(
            self.store.conn.execute(
                "select 1 from users where user_id = 'admin'"
            ).fetchone()
        )

    def test_reset_all_takes_a_backup_first(self):
        self.store.reset_all()
        backups = list(
            (self.store.db_path.parent / "backups").glob("*reset_all*.db")
        )
        self.assertEqual(len(backups), 1)

    def test_reset_all_requires_admin(self):
        from doctor_eval_app.models import User

        self.store.set_actor(User(user_id="evaluator_a", role="evaluator"))
        with self.assertRaises(PermissionError):
            self.store.reset_all()


class DemoFallbackTest(unittest.TestCase):
    """A missing dataset must fail loudly, never silently seed 4 demo samples
    that then stick permanently."""

    def test_missing_csv_raises_instead_of_seeding_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                with self.assertRaises(StartupError):
                    ensure_sample_data(store, Path(tmp))
                self.assertEqual(store.progress_summary()["total_samples"], 0)
            finally:
                store.close()

    def test_real_csv_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_dataset(base)
            store = create_store(base)
            try:
                ensure_sample_data(store, base)
                self.assertEqual(store.progress_summary()["total_samples"], 2)
            finally:
                store.close()

    def test_demo_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                ensure_sample_data(store, Path(tmp), allow_demo=True)
                self.assertEqual(store.progress_summary()["total_samples"], 4)
            finally:
                store.close()


class AtomicWriteAndCoerceTest(unittest.TestCase):
    def test_atomic_write_leaves_no_partial_file_and_no_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.json"
            _atomic_write_text(target, '{"a": 1}')
            self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_atomic_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "dump.json"
            target.write_text("old", encoding="utf-8")
            _atomic_write_text(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_coerce_int_never_raises(self):
        self.assertEqual(_coerce_int("5"), 5)
        self.assertEqual(_coerce_int("two"), 0)
        self.assertEqual(_coerce_int(None), 0)
        self.assertEqual(_coerce_int([1, 2]), 0)
        self.assertEqual(_coerce_int("bad", default=-1), -1)


def _qt_harness(test):
    """Return (QApplication classes, captured-windows list) or skip if no Qt."""
    try:
        from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
    except ModuleNotFoundError:  # pragma: no cover
        test.skipTest("PySide6 is not installed")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QApplication, QMainWindow, QMessageBox


class RecoveryReliabilityTest(unittest.TestCase):
    """Drive the real login path against planted dumps."""

    def _run_login(self, store, dumps):
        QApplication, QMainWindow, QMessageBox = _qt_harness(self)
        from doctor_eval_app.ui import run_app

        storage_dir = store.db_path.parent
        for name, content in dumps.items():
            (storage_dir / name).write_text(content, encoding="utf-8")

        captured = []
        orig_show = QMainWindow.show
        orig_exec = QApplication.exec
        orig_q = QMessageBox.question
        orig_i = QMessageBox.information
        QMainWindow.show = lambda w: (captured.append(w), orig_show(w))[1]
        QApplication.exec = lambda self: 0
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
        try:
            self.assertEqual(run_app(store), 0)
            window = captured[-1]
            window.login.account.setText("evaluator_a")
            window.login.password.setText("pass1234a")
            window.login.try_login()
            return window
        finally:
            QMainWindow.show = orig_show
            QApplication.exec = orig_exec
            QMessageBox.question = orig_q
            QMessageBox.information = orig_i

    def _store_with_samples(self, tmp):
        base = Path(tmp)
        _write_dataset(base)
        store = create_store(base)
        ensure_sample_data(store, base)
        return store

    def test_malformed_value_dump_does_not_brick_login(self):
        # Well-formed JSON, but a bad numeric value -- this used to crash the
        # form on the login path and strand the evaluator silently.
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_samples(tmp)
            try:
                dump = {
                    "evaluator_id": "evaluator_a", "sample_id": "1001",
                    "dumped_at": "2026-07-01T09:00:00", "scores": {},
                    "deductions": {"correctness": {"false_information": "two"}},
                    "detailed_comment": "recover me",
                    "timer": {"active_seconds": "oops"}, "llm_judge_view": {},
                }
                window = self._run_login(
                    store, {"unsaved-evaluator_a-bad-x.json": json.dumps(dump)}
                )
                view = window.stack.currentWidget()
                self.assertEqual(type(view).__name__, "EvaluatorView")
                self.assertEqual(view.comment.toPlainText(), "recover me")
            finally:
                store.close()

    def test_truncated_dump_is_quarantined_and_login_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store_with_samples(tmp)
            try:
                window = self._run_login(
                    store, {"unsaved-evaluator_a-trunc-x.json": "{ not json"}
                )
                self.assertEqual(
                    type(window.stack.currentWidget()).__name__, "EvaluatorView"
                )
                storage_dir = store.db_path.parent
                self.assertEqual(
                    len(list(storage_dir.glob("unsaved-*.json.corrupt"))), 1
                )
                self.assertEqual(len(list(storage_dir.glob("unsaved-*.json"))), 0)
            finally:
                store.close()


class AutosaveTest(unittest.TestCase):
    def test_autosave_draft_is_written_and_recovered_after_a_crash(self):
        QApplication, QMainWindow, QMessageBox = _qt_harness(self)
        from doctor_eval_app.ui import run_app

        captured = []
        orig_show = QMainWindow.show
        orig_exec = QApplication.exec
        orig_q = QMessageBox.question
        orig_i = QMessageBox.information
        QMainWindow.show = lambda w: (captured.append(w), orig_show(w))[1]
        QApplication.exec = lambda self: 0
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                _write_dataset(base)
                store = create_store(base)
                ensure_sample_data(store, base)
                storage_dir = store.db_path.parent
                try:
                    self.assertEqual(run_app(store), 0)
                    w = captured[-1]
                    w.login.account.setText("evaluator_a")
                    w.login.password.setText("pass1234a")
                    w.login.try_login()
                    ev = w.stack.currentWidget()
                    ev.start_pause_btn.click()
                    ev.comment.setPlainText("work in progress")
                    ev._autosave_draft()

                    drafts = list(storage_dir.glob("unsaved-*-draft.json"))
                    self.assertEqual(len(drafts), 1)
                    self.assertEqual(
                        json.loads(drafts[0].read_text())["detailed_comment"],
                        "work in progress",
                    )

                    # Simulate a crash: reopen the app without saving.
                    captured.clear()
                    self.assertEqual(run_app(store), 0)
                    w2 = captured[-1]
                    w2.login.account.setText("evaluator_a")
                    w2.login.password.setText("pass1234a")
                    w2.login.try_login()
                    ev2 = w2.stack.currentWidget()
                    self.assertEqual(ev2.comment.toPlainText(), "work in progress")
                finally:
                    store.close()
        finally:
            QMainWindow.show = orig_show
            QApplication.exec = orig_exec
            QMessageBox.question = orig_q
            QMessageBox.information = orig_i


if __name__ == "__main__":
    unittest.main()
