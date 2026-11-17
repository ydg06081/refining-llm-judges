import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from unittest import mock

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app import app as app_module
from doctor_eval_app import sample_data
from doctor_eval_app.app import (
    StartupError,
    create_store,
    ensure_demo_data,
    ensure_sample_data,
    import_additional_batches,
)
from doctor_eval_app.models import Sample
from doctor_eval_app.storage import pseudonymize_id
from doctor_eval_app.ui import (
    CORRECTNESS_COUNT_LABELS,
    CORRECTNESS_COUNT_MAX,
    EVALUATOR_CARD_MIN_HEIGHT,
    EVALUATOR_CARD_MIN_WIDTH,
    COURSE_MAX_HEIGHT,
    NOTE_MIN_HEIGHT,
    SAMPLE_NAV_LABELS,
    _format_score,
    _progress_percent,
    run_app,
)


ADMIN_TEST_PASSWORD = "admin-rotated-pw"


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 6, 12, 9, 0, 0).astimezone()

    def __call__(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class AppSmokeTest(unittest.TestCase):
    def test_ensure_demo_data_imports_samples_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                ensure_demo_data(store)
                ensure_demo_data(store)

                samples = store.list_samples_for_evaluator("evaluator_a")
            finally:
                store.close()

        self.assertEqual(len(samples), 4)
        self.assertIn("synthetic_01", {sample["sample_id"] for sample in samples})

    # --- top-up rounds ----------------------------------------------------

    def _write_round_csv(self, base_dir, filename, ids):
        data_dir = Path(base_dir) / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / filename
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write("sample_id,discharge_note_no_bhc,brief_hospital_course\n")
            for sample_id in ids:
                f.write(f"{sample_id},note {sample_id},course {sample_id}\n")
        return path

    ROUND_TWO_CSV = "round_two_top_up.csv"

    def _round_two(self, base_dir, ids):
        """Write a batch-2 CSV and register it for the duration of one test.

        ``ADDITIONAL_SAMPLE_CSVS`` is empty in the shipped config -- the current
        round is a single batch -- so these tests register their own entry
        rather than depending on one being configured. That keeps the top-up
        machinery covered whether or not a round is registered today.
        """
        patcher = mock.patch.object(
            sample_data, "ADDITIONAL_SAMPLE_CSVS", ((self.ROUND_TWO_CSV, 2),)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher_app = mock.patch.object(
            app_module, "ADDITIONAL_SAMPLE_CSVS", ((self.ROUND_TWO_CSV, 2),)
        )
        patcher_app.start()
        self.addCleanup(patcher_app.stop)
        return self._write_round_csv(base_dir, self.ROUND_TWO_CSV, ids)

    def test_top_up_round_is_imported_over_a_finished_database(self):
        """The upgrade path for an evaluator who has finished the first batch.

        They keep their storage/ folder and get a new exe plus the round-2 CSV.
        Nothing they already did may move.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample(f"base_{i:03d}", "note", "course", "") for i in range(1, 21)],
                    batch=1,
                )
                before = store.list_samples_for_evaluator("evaluator_a")

                self._round_two(tmp, [f"top_{i:03d}" for i in range(1, 31)])
                imported = import_additional_batches(store, Path(tmp))
                after = store.list_samples_for_evaluator("evaluator_a")
            finally:
                store.close()

        self.assertEqual([(f, b, n) for f, b, n in imported][0][1:], (2, 30))
        self.assertEqual(len(after), 50)
        self.assertEqual(
            [(r["sample_id"], r["display_order"]) for r in before],
            [(r["sample_id"], r["display_order"]) for r in after[:20]],
        )
        self.assertEqual([r["display_order"] for r in after[20:]], list(range(21, 51)))

    def test_top_up_import_is_idempotent_across_relaunches(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample("base_001", "note", "course", "")], batch=1
                )
                self._round_two(tmp, [f"top_{i:03d}" for i in range(1, 31)])

                first = import_additional_batches(store, Path(tmp))
                order_after_first = [
                    r["sample_id"] for r in store.list_samples_for_evaluator("evaluator_a")
                ]
                second = import_additional_batches(store, Path(tmp))
                third = import_additional_batches(store, Path(tmp))
                order_after_third = [
                    r["sample_id"] for r in store.list_samples_for_evaluator("evaluator_a")
                ]
            finally:
                store.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(third, [])
        self.assertEqual(order_after_first, order_after_third)

    def test_top_up_refuses_a_csv_that_repeats_first_round_samples(self):
        """Shipping the full combined file instead of the top-up-only one.

        The first-batch ids are already batch 1; importing them as batch 2 would
        renumber a round every evaluator has finished, so refuse to start.
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample(f"base_{i:03d}", "note", "course", "") for i in range(1, 6)],
                    batch=1,
                )
                self._round_two(tmp, ["base_002", "top_001"])

                with self.assertRaises(StartupError) as ctx:
                    import_additional_batches(store, Path(tmp))

                self.assertEqual(store.sample_batches(), {
                    1: [f"base_{i:03d}" for i in range(1, 6)]
                })
            finally:
                store.close()

        self.assertIn("base_002", str(ctx.exception))

    def test_top_up_refuses_to_grow_a_round_already_being_evaluated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample("base_001", "note", "course", "")], batch=1
                )
                store.import_samples(
                    [Sample("top_001", "note", "course", "")], batch=2
                )
                store.save_evaluation(
                    evaluator_id="evaluator_a",
                    sample_id="top_001",
                    display_order=2,
                    scores={
                        "completeness": 4,
                        "correctness": 1,
                        "readability": 3,
                        "overall_quality": 4,
                    },
                    deductions={"completeness": [], "correctness": {}, "readability": []},
                    detailed_comment="",
                    timer={"active_seconds": 10},
                    llm_judge_view={"opened": False},
                )
                self._round_two(tmp, ["top_001", "top_002"])

                with self.assertRaises(StartupError):
                    import_additional_batches(store, Path(tmp))

                self.assertEqual(len(store.sample_batches()[2]), 1)
            finally:
                store.close()

    def test_missing_top_up_csv_is_not_an_error(self):
        """Evaluators still on the old data folder must keep launching fine."""
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample("base_001", "note", "course", "")], batch=1
                )
                self.assertEqual(import_additional_batches(store, Path(tmp)), [])
                self.assertEqual(ensure_sample_data(store, Path(tmp)), [])
            finally:
                store.close()

    def test_fresh_install_loads_both_rounds_in_order(self):
        """A new evaluator joining later gets round 1 then round 2, not a mix."""
        with tempfile.TemporaryDirectory() as tmp:
            from doctor_eval_app.sample_data import REAL_SAMPLE_CSV_NAME

            self._write_round_csv(
                tmp, REAL_SAMPLE_CSV_NAME, [f"base_{i:03d}" for i in range(1, 21)]
            )
            self._round_two(tmp, [f"top_{i:03d}" for i in range(1, 31)])

            store = create_store(Path(tmp))
            try:
                ensure_sample_data(store, Path(tmp))
                rows = store.list_samples_for_evaluator("evaluator_a")
            finally:
                store.close()

        self.assertEqual(len(rows), 50)
        self.assertTrue(all(r["batch"] == 1 for r in rows[:20]))
        self.assertTrue(all(r["batch"] == 2 for r in rows[20:]))

    # --- wrong-round database guard ---------------------------------------

    def _round_folder(self, tmp, dataset_ids):
        """A distribution folder holding the configured dataset."""
        from doctor_eval_app.sample_data import REAL_SAMPLE_CSV_NAME

        (Path(tmp) / "storage").mkdir(parents=True, exist_ok=True)
        self._write_round_csv(tmp, REAL_SAMPLE_CSV_NAME, dataset_ids)

    def test_refuses_to_start_on_a_database_from_a_previous_round(self):
        """Copying a new build over an old folder must not pass silently.

        Sample import is guarded on an empty database, so the old round's
        samples stay and the new dataset never loads. The evaluator would see
        their finished previous round with no error at all.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._round_folder(tmp, [f"new_{i:03d}" for i in range(1, 31)])
            store = create_store(Path(tmp))
            try:
                store.import_samples(
                    [Sample(f"old_{i:03d}", "note", "course", "") for i in range(1, 11)],
                    batch=1,
                )
                with self.assertRaises(StartupError) as ctx:
                    ensure_sample_data(store, Path(tmp))
            finally:
                store.close()

        message = str(ctx.exception)
        self.assertIn("맞지 않습니다", message)
        # It must tell them to keep the old folder -- it holds their results.
        self.assertIn("보관", message)

    def test_starts_normally_when_the_database_matches_the_dataset(self):
        ids = [f"new_{i:03d}" for i in range(1, 31)]
        with tempfile.TemporaryDirectory() as tmp:
            self._round_folder(tmp, ids)

            store = create_store(Path(tmp))          # first launch: imports
            try:
                ensure_sample_data(store, Path(tmp))
                first = len(store.list_samples_for_evaluator("evaluator_a"))
            finally:
                store.close()

            store = create_store(Path(tmp))          # relaunch: must not refuse
            try:
                ensure_sample_data(store, Path(tmp))
                second = len(store.list_samples_for_evaluator("evaluator_a"))
            finally:
                store.close()

        self.assertEqual((first, second), (30, 30))

    def test_a_top_up_batch_does_not_trip_the_wrong_round_guard(self):
        """Sharing any sample_id with the dataset means the same round."""
        with tempfile.TemporaryDirectory() as tmp:
            self._round_folder(tmp, [f"new_{i:03d}" for i in range(1, 31)])
            store = create_store(Path(tmp))
            try:
                ensure_sample_data(store, Path(tmp))
                store.import_samples([Sample("extra_001", "n", "c", "")], batch=2)
                ensure_sample_data(store, Path(tmp))  # must not raise
                total = len(store.list_samples_for_evaluator("evaluator_a"))
            finally:
                store.close()

        self.assertEqual(total, 31)

    def test_missing_dataset_does_not_block_a_populated_folder(self):
        """No dataset to compare against is not a mismatch."""
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                store.import_samples([Sample("x1", "note", "course", "")], batch=1)
                ensure_sample_data(store, Path(tmp))  # must not raise
                total = len(store.list_samples_for_evaluator("evaluator_a"))
            finally:
                store.close()

        self.assertEqual(total, 1)

    def test_ui_module_exposes_run_app_without_importing_qt(self):
        self.assertTrue(callable(run_app))

    def test_reader_layout_prioritizes_discharge_note_space(self):
        self.assertGreaterEqual(NOTE_MIN_HEIGHT, COURSE_MAX_HEIGHT * 2)

    def test_sample_navigation_labels_use_arrows(self):
        self.assertEqual(SAMPLE_NAV_LABELS, {"previous": "←", "next": "→"})

    def test_admin_score_formatting(self):
        self.assertEqual(_format_score(None), "-")
        self.assertEqual(_format_score(4), "4.00")
        self.assertEqual(_format_score(3.5), "3.50")

    def test_admin_progress_percent_formatting(self):
        self.assertEqual(_progress_percent(0, 0), 0)
        self.assertEqual(_progress_percent(0, 100), 0)
        self.assertEqual(_progress_percent(12, 100), 12)
        self.assertEqual(_progress_percent(1, 3), 33)

    def test_flagged_admin_password_cannot_reach_the_dashboard(self):
        """Logging in with an admin password flagged for rotation must stop at
        the forced rotation dialog; cancelling it returns to login, not the
        dashboard."""
        try:
            from PySide6.QtWidgets import QApplication, QDialog, QMainWindow
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_dialog_exec = QDialog.exec

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        # Stand in for the clinician dismissing the rotation dialog.
        QDialog.exec = lambda self: QDialog.Rejected
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]

                    window.login.account.setText("admin")
                    window.login.password.setText("admin")
                    window.login.try_login()

                    # Bounced back to login, and the session holds no authority.
                    self.assertIs(window.stack.currentWidget(), window.login)
                    self.assertIsNone(window.current_user)
                    self.assertIsNone(store._actor)

                    # Even a dialog that reports success must not admit the
                    # session while the stored password is still the default.
                    QDialog.exec = lambda self: QDialog.Accepted
                    window.login.account.setText("admin")
                    window.login.password.setText("admin")
                    window.login.try_login()
                    self.assertIs(window.stack.currentWidget(), window.login)
                    self.assertIsNone(window.current_user)
                    self.assertIsNotNone(store.authenticate("admin", "admin"))

                    # Rotating the password clears the block.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)
                    window.login.account.setText("admin")
                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    self.assertIsNot(window.stack.currentWidget(), window.login)
                    self.assertEqual(window.current_user.user_id, "admin")
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QDialog.exec = original_dialog_exec

    def test_evaluator_session_binds_actor_and_logout_clears_it(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "j")])
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]

                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    self.assertEqual(store._actor.user_id, "evaluator_a")
                    # An evaluator session cannot read across evaluators.
                    with self.assertRaises(PermissionError):
                        store.export_rows()

                    window.show_login()
                    self.assertIsNone(store._actor)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec

    def test_admin_dashboard_uses_large_cards(self):
        self.assertGreaterEqual(EVALUATOR_CARD_MIN_WIDTH, 300)
        self.assertGreaterEqual(EVALUATOR_CARD_MIN_HEIGHT, 240)

    def test_correctness_uses_count_controls_with_room_above_score_floor(self):
        self.assertEqual(
            set(CORRECTNESS_COUNT_LABELS),
            {"false_information", "causal_error"},
        )
        self.assertGreater(CORRECTNESS_COUNT_MAX, 4)

    def test_brief_hospital_course_owns_a_column_and_no_judge_panel_is_shown(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QSplitter
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [Sample("sample_001", "note", "course body", "judge text")]
                    )

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # The LLM-as-Judge panel is gone from the evaluation screen.
                    self.assertFalse(hasattr(evaluator, "judge"))
                    self.assertFalse(hasattr(evaluator, "judge_toggle"))
                    self.assertEqual(evaluator.llm_open_count, 0)
                    self.assertIsNone(evaluator.llm_first_opened_at)

                    # Brief Hospital Course sits in the column the judge used to
                    # own, beside the note rather than squeezed under it.
                    splitter = evaluator.findChild(QSplitter)
                    self.assertEqual(splitter.count(), 4)
                    self.assertIs(evaluator.course.parentWidget(), splitter.widget(2))
                    self.assertEqual(evaluator.course.toPlainText(), "course body")

                    # No short fixed-height cap any more: it fills its column.
                    self.assertGreater(evaluator.course.maximumHeight(), 10000)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec

    def test_llm_judge_view_records_never_shown_for_new_evaluations(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge text"),
                            Sample("sample_002", "note 2", "course 2", ""),
                        ]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    clock.advance(6)
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()

                    store.set_actor(None)  # inspect as the harness, not as the logged-in user

                    # The panel is gone, so every new row records "never shown".
                    # The columns stay, so previously saved rows keep their values.
                    row = store.export_rows()[0]
                    self.assertEqual(row["llm_judge_opened"], 0)
                    self.assertEqual(row["llm_judge_open_count"], 0)
                    self.assertEqual(row["llm_judge_visible_seconds"], 0)
                    self.assertIsNone(row["llm_judge_first_opened_at"])
                    self.assertEqual(row["active_seconds"], 6)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info

    def test_timer_reset_clears_timing_and_llm_judge_tracking_state(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge text"),
                            Sample("sample_002", "note 2", "course 2", ""),
                        ]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    clock.advance(5)
                    evaluator.reset_btn.click()

                    self.assertEqual(evaluator.llm_open_count, 0)
                    self.assertIsNone(evaluator.llm_last_opened)

                    evaluator.start_pause_btn.click()
                    clock.advance(2)
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()

                    store.set_actor(None)  # inspect as the harness, not as the logged-in user

                    row = store.export_rows()[0]
                    self.assertEqual(row["llm_judge_opened"], 0)
                    self.assertEqual(row["llm_judge_visible_seconds"], 0)
                    self.assertEqual(row["reset_count"], 1)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_timer_pause_resume_uses_wall_clock_time_when_saved(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge text"),
                            Sample("sample_002", "note 2", "course 2", ""),
                        ]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    clock.advance(5)
                    evaluator.tick()
                    evaluator.start_pause_btn.click()
                    clock.advance(10)
                    evaluator.start_pause_btn.click()
                    clock.advance(4)
                    evaluator.tick()
                    self.assertEqual(evaluator.timer_display.text(), "00:09")
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()

                    store.set_actor(None)  # inspect as the harness, not as the logged-in user

                    row = store.export_rows()[0]
                    self.assertEqual(row["active_seconds"], 9)
                    self.assertEqual(row["paused_seconds"], 10)
                    self.assertEqual(row["pause_count"], 1)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info

    def test_timer_reset_preserves_reset_count_and_restarts_clock(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge text"),
                            Sample("sample_002", "note 2", "course 2", ""),
                        ]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    clock.advance(3)
                    evaluator.tick()
                    evaluator.reset_btn.click()

                    evaluator.start_pause_btn.click()
                    clock.advance(2)
                    evaluator.tick()
                    evaluator.start_pause_btn.click()
                    self.assertEqual(evaluator.timer_display.text(), "00:02")
                    clock.advance(7)
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()

                    store.set_actor(None)  # inspect as the harness, not as the logged-in user

                    row = store.export_rows()[0]
                    self.assertEqual(row["active_seconds"], 2)
                    self.assertEqual(row["paused_seconds"], 7)
                    self.assertEqual(row["pause_count"], 1)
                    self.assertEqual(row["reset_count"], 1)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_editing_completed_evaluation_keeps_values_and_continues_timer(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        questions = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        def record_question(*args, **kwargs):
            questions.append(args)
            return QMessageBox.Ok

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = record_question
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge text")])
                    store.save_evaluation(
                        evaluator_id="evaluator_a",
                        sample_id="sample_001",
                        display_order=1,
                        scores={
                            "completeness": 5,
                            "correctness": 5,
                            "readability": 5,
                            "overall_quality": 5,
                        },
                        deductions={
                            "completeness": [],
                            "correctness": {},
                            "readability": [],
                        },
                        detailed_comment="saved evaluation",
                        timer={
                            "started_at": "2026-06-12T09:00:00+09:00",
                            "ended_at": "2026-06-12T09:01:00+09:00",
                            "active_seconds": 60,
                            "paused_seconds": 0,
                            "pause_count": 0,
                            "reset_count": 0,
                        },
                        llm_judge_view={},
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # Landing on a completed sample shows saved values read-only,
                    # with the timer control relabelled to Edit and Reset enabled.
                    self.assertEqual(evaluator.comment.toPlainText(), "saved evaluation")
                    self.assertEqual(evaluator.start_pause_btn.text(), "Edit")
                    self.assertTrue(evaluator.reset_btn.isEnabled())
                    self.assertEqual(evaluator.elapsed_seconds, 60)

                    # Edit keeps the loaded values and runs the timer: time flows,
                    # continuing from the recorded 60s.
                    evaluator.start_pause_btn.click()
                    self.assertIsNotNone(evaluator.started_at)
                    self.assertTrue(evaluator.timer.isActive())
                    self.assertEqual(evaluator.start_pause_btn.text(), "Pause")
                    self.assertEqual(evaluator.comment.toPlainText(), "saved evaluation")
                    self.assertTrue(evaluator.comment.isEnabled())

                    # Overwrite the comment; advancing the clock adds to the
                    # recorded active time instead of freezing it.
                    evaluator.comment.setPlainText("updated note")
                    clock.advance(5)
                    evaluator.end_btn.click()

                    saved = store.get_evaluation("evaluator_a", "sample_001")
                    self.assertEqual(saved["detailed_comment"], "updated note")
                    self.assertEqual(saved["active_seconds"], 65)
                    self.assertEqual(saved["scores"]["overall_quality"], 5)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_reset_always_available_and_returns_to_unevaluated_state(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        questions = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        def record_question(*args, **kwargs):
            questions.append(args)
            return QMessageBox.Ok

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = record_question
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge text")])
                    store.save_evaluation(
                        evaluator_id="evaluator_a",
                        sample_id="sample_001",
                        display_order=1,
                        scores={
                            "completeness": 5,
                            "correctness": 5,
                            "readability": 5,
                            "overall_quality": 5,
                        },
                        deductions={
                            "completeness": [],
                            "correctness": {},
                            "readability": [],
                        },
                        detailed_comment="saved evaluation",
                        timer={
                            "started_at": "2026-06-12T09:00:00+09:00",
                            "ended_at": "2026-06-12T09:01:00+09:00",
                            "active_seconds": 60,
                            "paused_seconds": 0,
                            "pause_count": 0,
                            "reset_count": 0,
                        },
                        llm_judge_view={},
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # Reset is available even before entering edit mode.
                    self.assertEqual(evaluator.start_pause_btn.text(), "Edit")
                    self.assertTrue(evaluator.reset_btn.isEnabled())

                    # Reset from the read-only view returns the sample to a
                    # pre-evaluation state: cleared form, zeroed timer, locked.
                    evaluator.reset_btn.click()
                    self.assertIsNone(evaluator.started_at)
                    self.assertEqual(evaluator.comment.toPlainText(), "")
                    self.assertEqual(evaluator.overall.currentData(), "")
                    self.assertEqual(evaluator.elapsed_seconds, 0)
                    self.assertEqual(evaluator.timer_display.text(), "00:00")
                    self.assertEqual(evaluator.start_pause_btn.text(), "Start")
                    self.assertFalse(evaluator.comment.isEnabled())
                    self.assertTrue(evaluator.reset_btn.isEnabled())
                    # Reset is a true reset: the saved DB record is removed so the
                    # sample returns to the un-evaluated state (no ✓, blank on revisit).
                    self.assertIsNone(store.get_evaluation("evaluator_a", "sample_001"))
                    self.assertNotIn("✓", evaluator.sample_list.item(0).text())
                    self.assertFalse(evaluator.samples[0]["completed"])
                    self.assertEqual(evaluator.progress_label.text(), "0 / 1 완료")

                    # Re-evaluate from scratch: time is measured from zero.
                    evaluator.start_pause_btn.click()
                    self.assertTrue(evaluator.timer.isActive())
                    clock.advance(3)
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.comment.setPlainText("after reset")
                    evaluator.end_btn.click()

                    saved = store.get_evaluation("evaluator_a", "sample_001")
                    self.assertEqual(saved["detailed_comment"], "after reset")
                    self.assertEqual(saved["active_seconds"], 3)
                    self.assertEqual(saved["reset_count"], 1)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_reset_persists_and_clears_checkmark_after_navigating_away(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note 1", "course 1", "judge 1"),
                            Sample("sample_002", "note 2", "course 2", "judge 2"),
                        ]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # Complete the sample sitting at row 0, then return to it.
                    first_sample = evaluator.samples[0]["sample_id"]
                    evaluator.start_pause_btn.click()
                    clock.advance(4)
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.comment.setPlainText("saved evaluation")
                    evaluator.end_btn.click()
                    evaluator.sample_list.setCurrentRow(0)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertEqual(evaluator.start_pause_btn.text(), "Edit")
                    self.assertIn("✓", evaluator.sample_list.item(0).text())

                    # Reset must remove the saved evaluation entirely.
                    evaluator.reset_btn.click()
                    self.assertIsNone(store.get_evaluation("evaluator_a", first_sample))
                    self.assertNotIn("✓", evaluator.sample_list.item(0).text())
                    self.assertEqual(evaluator.progress_label.text(), "0 / 2 완료")

                    # Navigate away to the other sample and come back: the reset
                    # must persist — the form stays blank, not the pre-reset state.
                    evaluator.sample_list.setCurrentRow(1)
                    self.assertEqual(evaluator.current_index, 1)
                    evaluator.sample_list.setCurrentRow(0)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertIn(first_sample, evaluator.sample_title.text())
                    self.assertEqual(evaluator.comment.toPlainText(), "")
                    self.assertEqual(evaluator.overall.currentData(), "")
                    self.assertEqual(evaluator.start_pause_btn.text(), "Start")
                    self.assertFalse(
                        any(
                            check.isChecked()
                            for _, check in evaluator.metric_checks["completeness"]
                        )
                    )
                    self.assertNotIn("✓", evaluator.sample_list.item(0).text())
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_reset_on_recovered_sample_discards_dump_and_persists(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        def smart_question(*args, **kwargs):
            # Reset confirm uses Ok/Cancel; recovery prompt uses Yes/No.
            title = args[1] if len(args) > 1 else ""
            return QMessageBox.Ok if "초기화" in title else QMessageBox.Yes

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *a, **k: QMessageBox.Ok
        QMessageBox.question = smart_question
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge"),
                            Sample("sample_002", "note 2", "course 2", "judge 2"),
                        ]
                    )
                    samples = store.list_samples_for_evaluator("evaluator_a")
                    target = samples[0]["sample_id"]
                    dump = {
                        "evaluator_id": "evaluator_a",
                        "sample_id": target,
                        "display_order": 1,
                        "scores": {"completeness": 5, "correctness": 5, "readability": 5, "overall_quality": 3},
                        "deductions": {"completeness": [], "correctness": {}, "readability": []},
                        "detailed_comment": "RECOVERED",
                        "timer": {},
                        "llm_judge_view": {},
                        "dumped_at": "2026-06-18T00:00:00+09:00",
                    }
                    dump_file = Path(tmp) / "storage" / f"unsaved-evaluator_a-{target}-x.json"
                    dump_file.write_text(json.dumps(dump), encoding="utf-8")

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # Recovery dump was applied to the form.
                    self.assertIn(target, evaluator._recovery_dumps)
                    self.assertEqual(evaluator.comment.toPlainText(), "RECOVERED")

                    # Reset must discard the recovery dump AND delete its file.
                    evaluator.reset_btn.click()
                    self.assertNotIn(target, evaluator._recovery_dumps)
                    self.assertFalse(dump_file.exists())
                    self.assertNotIn("⟳", evaluator.sample_list.item(0).text())
                    self.assertEqual(evaluator.comment.toPlainText(), "")

                    # Navigate away and back: reset persists, the dump is not re-applied.
                    evaluator.sample_list.setCurrentRow(1)
                    evaluator.sample_list.setCurrentRow(0)
                    self.assertEqual(evaluator.comment.toPlainText(), "")
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_logout_with_unset_overall_stays_if_backup_dump_fails(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.question = lambda *a, **k: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()  # in-progress, Overall unset
                    # Simulate the worst case: even the backup dump fails (the
                    # clipboard fallback was shown). The user must NOT be logged
                    # out, so the clipboard-only copy is not silently lost.
                    evaluator._record_unsaved_dump = lambda *a, **k: None
                    called = []
                    evaluator._escape_after_dump(lambda: called.append(True))
                    self.assertEqual(called, [])
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.question = original_question

    def _patch_dialogs(self, dialog_exec=None):
        """Capture the main window and silence modal boxes for one test.

        Returns the list the shown windows are appended to. ``dialog_exec``
        replaces QDialog.exec; the default fails the test, so an unexpected
        blocking dialog (e.g. a forced password change) cannot pass unnoticed.
        """
        from PySide6.QtWidgets import QApplication, QDialog, QMainWindow, QMessageBox

        captured_windows = []
        originals = {
            (QMainWindow, "show"): QMainWindow.show,
            (QApplication, "exec"): QApplication.exec,
            (QDialog, "exec"): QDialog.exec,
            (QMessageBox, "information"): QMessageBox.information,
            (QMessageBox, "warning"): QMessageBox.warning,
            (QMessageBox, "question"): QMessageBox.question,
        }
        original_show = QMainWindow.show

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        def unexpected_dialog(dialog):
            self.fail(f"unexpected modal dialog: {type(dialog).__name__}")

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QDialog.exec = dialog_exec or unexpected_dialog
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes

        def restore():
            for (owner, name), value in originals.items():
                setattr(owner, name, value)

        self.addCleanup(restore)
        return captured_windows

    def _account_free_store(self, tmp):
        """A store exactly as the shipped app creates it: no accounts at all."""
        from doctor_eval_app.storage import DoctorEvalStore

        store = DoctorEvalStore(Path(tmp) / "storage" / "doctor_eval.db")
        store.seed_accounts = ()
        store.initialize()
        ensure_demo_data(store)
        return store

    def test_first_launch_creates_admin_then_admin_issues_evaluator_account(self):
        try:
            from PySide6.QtWidgets import QDialog  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        new_evaluator = {
            "account": "kim_eval",
            "name": "Kim Evaluator",
            "password": "kim-pass-1",
        }

        def fill_create_dialog(dialog):
            self.assertEqual(type(dialog).__name__, "CreateEvaluatorDialog")
            dialog.account.setText(new_evaluator["account"])
            dialog.name.setText(new_evaluator["name"])
            dialog.password.setText(new_evaluator["password"])
            dialog.password_check.setText(new_evaluator["password"])
            dialog.submit()
            return dialog.result()

        captured_windows = self._patch_dialogs(dialog_exec=fill_create_dialog)
        with tempfile.TemporaryDirectory() as tmp:
            store = self._account_free_store(tmp)
            try:
                self.assertFalse(store.has_admin())
                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]

                # No admin yet: the app opens on admin setup, not on login.
                setup = window.stack.currentWidget()
                self.assertEqual(type(setup).__name__, "AdminSetupView")

                # Invalid input keeps the screen up with an inline error.
                setup.account.setText("lab_admin")
                setup.password.setText("short")
                setup.password_check.setText("short")
                setup.submit()
                self.assertIs(window.stack.currentWidget(), setup)
                self.assertIn("at least", setup.error_label.text())
                self.assertFalse(store.has_admin())

                setup.password.setText("lab-admin-pw")
                setup.password_check.setText("lab-admin-pw")
                setup.submit()

                # Setup is gone; login is shown with the new admin ID filled in.
                self.assertIs(window.stack.currentWidget(), window.login)
                self.assertEqual(window.login.account.text(), "lab_admin")
                self.assertEqual(window.login.password.text(), "")
                self.assertTrue(store.has_admin())
                remaining = [
                    type(window.stack.widget(i)).__name__
                    for i in range(window.stack.count())
                ]
                self.assertNotIn("AdminSetupView", remaining)

                # The chosen password needs no forced rotation (any dialog
                # other than the create dialog would fail the test).
                window.login.password.setText("lab-admin-pw")
                window.login.try_login()
                admin = window.stack.currentWidget()
                self.assertEqual(type(admin).__name__, "AdminView")
                self.assertEqual(admin.account_count_label.text(), "Evaluator accounts: 0")

                admin.open_create_evaluator_dialog()

                self.assertEqual(admin.account_count_label.text(), "Evaluator accounts: 1")
                self.assertEqual(
                    [u.user_id for u in store.list_users(role="evaluator")],
                    ["kim_eval"],
                )

                # The evaluator can sign in straight away -- no approval step.
                window.show_login()
                window.login.account.setText("kim_eval")
                window.login.password.setText("kim-pass-1")
                window.login.try_login()
                evaluator = window.stack.currentWidget()
                self.assertEqual(type(evaluator).__name__, "EvaluatorView")
                window.show_login()

                # The next launch goes straight to login.
                self.assertEqual(run_app(store), 0)
                self.assertIs(captured_windows[-1].stack.currentWidget(), captured_windows[-1].login)
            finally:
                store.close()

    def test_setup_screen_hands_over_to_login_when_admin_already_exists(self):
        try:
            from PySide6.QtWidgets import QDialog  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        captured_windows = self._patch_dialogs()
        with tempfile.TemporaryDirectory() as tmp:
            store = self._account_free_store(tmp)
            try:
                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]
                setup = window.stack.currentWidget()
                self.assertEqual(type(setup).__name__, "AdminSetupView")

                # Another window on the same folder finished setup first.
                store.create_initial_admin("first_adm", "pw-123456", "pw-123456")

                setup.account.setText("second_adm")
                setup.password.setText("pw-654321")
                setup.password_check.setText("pw-654321")
                setup.submit()

                self.assertIs(window.stack.currentWidget(), window.login)
                self.assertEqual(window.login.account.text(), "")
                self.assertIsNone(store.authenticate("second_adm", "pw-654321"))
                self.assertEqual(
                    [u.user_id for u in store.list_users(role="admin")], ["first_adm"]
                )
            finally:
                store.close()

    def test_create_evaluator_dialog_reports_errors_and_creates_nothing(self):
        try:
            from PySide6.QtWidgets import QDialog  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        seen_errors = []

        def duplicate_then_cancel(dialog):
            dialog.account.setText("evaluator_a")
            dialog.password.setText("pw-123456")
            dialog.password_check.setText("pw-123456")
            dialog.submit()
            seen_errors.append(dialog.error_label.text())
            dialog.reject()
            return dialog.result()

        captured_windows = self._patch_dialogs(dialog_exec=duplicate_then_cancel)
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                ensure_demo_data(store)
                store.set_password("admin", ADMIN_TEST_PASSWORD)
                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]
                window.login.account.setText("admin")
                window.login.password.setText(ADMIN_TEST_PASSWORD)
                window.login.try_login()
                admin = window.stack.currentWidget()

                admin.open_create_evaluator_dialog()

                self.assertEqual(seen_errors, ["ID is already registered"])
                self.assertEqual(admin.account_count_label.text(), "Evaluator accounts: 3")
                self.assertIsNotNone(store.authenticate("evaluator_a", "pass1234a"))
            finally:
                store.close()

    def test_admin_can_reset_an_evaluator_password_from_dashboard(self):
        try:
            from PySide6.QtWidgets import QDialog  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        def set_new_password(dialog):
            self.assertEqual(type(dialog).__name__, "PasswordChangeDialog")
            self.assertEqual(dialog.user.user_id, "evaluator_b")
            dialog.password.setText("reset-pass-b")
            dialog.password_check.setText("reset-pass-b")
            dialog.submit()
            return dialog.result()

        captured_windows = self._patch_dialogs(dialog_exec=set_new_password)
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                ensure_demo_data(store)
                store.set_password("admin", ADMIN_TEST_PASSWORD)
                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]
                window.login.account.setText("admin")
                window.login.password.setText(ADMIN_TEST_PASSWORD)
                window.login.try_login()
                admin = window.stack.currentWidget()
                target = next(
                    u for u in store.list_users(role="evaluator") if u.user_id == "evaluator_b"
                )

                admin.reset_evaluator_password(target)

                self.assertIsNone(store.authenticate("evaluator_b", "pass1234b"))
                self.assertIsNotNone(store.authenticate("evaluator_b", "reset-pass-b"))
            finally:
                store.close()

    def test_login_screen_offers_no_signup(self):
        try:
            from PySide6.QtWidgets import QPushButton
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        captured_windows = self._patch_dialogs()
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]
                self.assertIs(window.stack.currentWidget(), window.login)
                button_texts = {
                    button.text() for button in window.login.findChildren(QPushButton)
                }
                self.assertNotIn("Evaluator Signup", button_texts)
                self.assertFalse(any("signup" in t.lower() for t in button_texts))
                self.assertFalse(hasattr(window.login, "show_signup"))
                self.assertFalse(hasattr(window, "signup"))
            finally:
                store.close()

    def test_legacy_rejected_account_gets_inactive_login_message(self):
        try:
            from PySide6.QtWidgets import QPushButton  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        captured_windows = self._patch_dialogs()
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(Path(tmp))
            try:
                # A row left over from the old self-signup flow.
                store.create_evaluator_account("rejected_eval", "pw-123456", "pw-123456")
                with store.conn:
                    store.conn.execute(
                        "update users set status = 'rejected' where user_id = 'rejected_eval'"
                    )

                self.assertEqual(run_app(store), 0)
                window = captured_windows[-1]
                window.login.account.setText("rejected_eval")
                window.login.password.setText("pw-123456")
                window.login.try_login()

                self.assertIs(window.stack.currentWidget(), window.login)
                self.assertEqual(
                    window.login.error_label.text(),
                    "This account is not active. Contact your admin.",
                )
            finally:
                store.close()

    def test_admin_can_delete_evaluator_account(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_warning = QMessageBox.warning
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]

                    # Rotate off the fixture default first: it is flagged
                    # must_change_password, which would open a blocking dialog.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)

                    window.login.account.setText("admin")

                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    admin = window.stack.currentWidget()
                    self.assertEqual(admin.account_count_label.text(), "Evaluator accounts: 3")

                    admin.delete_evaluator_account("evaluator_a")

                    self.assertIsNone(store.authenticate("evaluator_a", "pass1234a"))
                    self.assertEqual(
                        [user.user_id for user in store.list_users(role="evaluator")],
                        ["evaluator_b", "evaluator_c"],
                    )
                    self.assertEqual(admin.account_count_label.text(), "Evaluator accounts: 2")
                    self.assertNotIn("evaluator_a", admin.evaluator_summary.text())
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.warning = original_warning
            QMessageBox.question = original_question

    def test_admin_header_has_no_eval_shortcut_or_signup_notifications(self):
        try:
            from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMessageBox, QPushButton
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_warning = QMessageBox.warning

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]

                    # Rotate off the fixture default first: it is flagged
                    # must_change_password, which would open a blocking dialog.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)

                    window.login.account.setText("admin")

                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    admin = window.stack.currentWidget()

                    button_texts = {button.text() for button in admin.findChildren(QPushButton)}
                    self.assertNotIn("Evaluate as approved evaluator", button_texts)
                    # Signup requests no longer exist, so neither does the bell.
                    self.assertFalse(hasattr(admin, "pending_dropdown"))
                    self.assertFalse(any(text.startswith("🔔") for text in button_texts))
                    self.assertIn("Add evaluator", button_texts)
                    self.assertIn("Export CSV", button_texts)
                    self.assertIn("Export JSONL", button_texts)
                    self.assertIn("Reset", button_texts)
                    self.assertIn("Hide", button_texts)
                    self.assertEqual(
                        [button.text() for button in admin.findChildren(QPushButton)].count("Review"),
                        1,
                    )
                    self.assertTrue(admin.account_list.isVisible())
                    admin.account_toggle_btn.click()
                    self.assertFalse(admin.account_list.isVisible())
                    self.assertEqual(admin.account_toggle_btn.text(), "Show")
                    admin.account_toggle_btn.click()
                    self.assertTrue(admin.account_list.isVisible())
                    self.assertEqual(admin.account_toggle_btn.text(), "Hide")
                    label_texts = {label.text() for label in admin.findChildren(QLabel)}
                    self.assertIn("Data actions", label_texts)
                    self.assertIn("Evaluator Accounts", label_texts)
                    self.assertIn("Evaluator Score Summary", label_texts)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.warning = original_warning

    def test_admin_can_review_sample_comparison_read_only(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_warning = QMessageBox.warning

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note 1", "course 1", "judge 1"),
                            Sample("sample_002", "note 2", "course 2", "judge 2"),
                        ]
                    )
                    store.save_evaluation(
                        evaluator_id="evaluator_a",
                        sample_id="sample_001",
                        display_order=1,
                        scores={
                            "completeness": 4,
                            "correctness": 2,
                            "readability": 5,
                            "overall_quality": 3,
                        },
                        deductions={
                            "completeness": ["missing_condition"],
                            "correctness": {"false_information": 2, "causal_error": 1},
                            "readability": [],
                        },
                        detailed_comment="needs source check",
                        timer={},
                        llm_judge_view={},
                    )
                    store.save_evaluation(
                        evaluator_id="evaluator_b",
                        sample_id="sample_001",
                        display_order=1,
                        scores={
                            "completeness": 5,
                            "correctness": 5,
                            "readability": 4,
                            "overall_quality": 4,
                        },
                        deductions={"completeness": [], "correctness": {}, "readability": []},
                        detailed_comment="ready for discussion",
                        timer={},
                        llm_judge_view={},
                    )
                    store.save_evaluation(
                        evaluator_id="evaluator_b",
                        sample_id="sample_002",
                        display_order=2,
                        scores={
                            "completeness": 1,
                            "correctness": 1,
                            "readability": 1,
                            "overall_quality": 1,
                        },
                        deductions={"completeness": ["missing_reason"], "correctness": {}, "readability": []},
                        detailed_comment="draft must not leak",
                        timer={},
                        llm_judge_view={},
                        completed=False,
                    )

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    # Rotate off the fixture default first: it is flagged
                    # must_change_password, which would open a blocking dialog.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)
                    window.login.account.setText("admin")
                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    admin = window.stack.currentWidget()

                    review_buttons = [
                        button
                        for button in admin.findChildren(QPushButton)
                        if button.text() == "Review"
                    ]
                    self.assertEqual(len(review_buttons), 1)
                    review_buttons[0].click()
                    review = window.stack.currentWidget()

                    self.assertEqual(review.__class__.__name__, "AdminSampleComparisonReviewView")
                    self.assertEqual(review.sample_list.count(), 2)
                    self.assertTrue(review.sample_list.isVisible())
                    self.assertIn("sample_001", review.sample_title.text())
                    self.assertIn("1 / 2 · sample_001", review.sample_counter.text())
                    self.assertFalse(review.previous_btn.isEnabled())
                    self.assertTrue(review.next_btn.isEnabled())
                    self.assertEqual(review.note.toPlainText(), "note 1")
                    self.assertEqual(review.course.toPlainText(), "course 1")
                    # The LLM-as-Judge panel is gone from this screen too; the
                    # sample's judge text is still stored, just not shown.
                    self.assertFalse(hasattr(review, "judge"))
                    self.assertTrue(review.note.isReadOnly())
                    self.assertTrue(review.course.isReadOnly())
                    self.assertEqual(len(review.reviewer_columns), 3)
                    self.assertEqual(
                        list(review.reviewer_status_labels),
                        ["evaluator_a", "evaluator_b", "evaluator_c"],
                    )
                    self.assertEqual(review.reviewer_status_labels["evaluator_a"].text(), "Completed")
                    self.assertEqual(review.reviewer_status_labels["evaluator_b"].text(), "Completed")
                    self.assertEqual(review.reviewer_status_labels["evaluator_c"].text(), "Not completed")
                    self.assertEqual(review.score_values["evaluator_a"]["completeness"].text(), "Score: 4")
                    self.assertEqual(
                        review.score_values["evaluator_b"]["correctness"].text(),
                        "Score: 5 · Errors: 0 · max -4",
                    )
                    self.assertEqual(review.score_values["evaluator_c"]["overall_quality"].currentData(), "")
                    self.assertFalse(review.score_values["evaluator_c"]["overall_quality"].isEnabled())
                    self.assertEqual(
                        review.correctness_count_inputs[("evaluator_a", "false_information")].value(),
                        2,
                    )
                    self.assertFalse(
                        review.correctness_count_inputs[("evaluator_a", "false_information")].isEnabled()
                    )
                    self.assertEqual(review.comment_boxes["evaluator_a"].toPlainText(), "needs source check")
                    self.assertEqual(review.comment_boxes["evaluator_b"].toPlainText(), "ready for discussion")
                    self.assertTrue(review.comment_boxes["evaluator_a"].isReadOnly())
                    self.assertEqual(
                        list(review.completeness_row_widgets),
                        [
                            "missing_reason",
                            "missing_condition",
                            "missing_treatment",
                            "course_not_documented",
                        ],
                    )
                    self.assertEqual(len(review.completeness_cells), 4 * len(review.reviewer_columns))
                    self.assertTrue(
                        review.completeness_cells[("missing_condition", "evaluator_a")].isChecked()
                    )
                    self.assertFalse(
                        review.completeness_cells[("missing_condition", "evaluator_b")].isChecked()
                    )
                    self.assertFalse(
                        review.completeness_cells[("missing_condition", "evaluator_c")].isChecked()
                    )
                    self.assertEqual(
                        review.deduction_labels[("evaluator_a", "completeness", "missing_condition")].text(),
                        "Condition missing",
                    )
                    self.assertEqual(
                        review.deduction_labels[("evaluator_c", "completeness", "missing_condition")].toolTip(),
                        "Not completed",
                    )
                    self.assertIn(
                        "#fff4d6",
                        review.completeness_row_widgets["missing_condition"][0].styleSheet(),
                    )
                    self.assertNotIn(
                        "#fff4d6",
                        review.completeness_row_widgets["missing_reason"][0].styleSheet(),
                    )
                    review_button_texts = {button.text() for button in review.findChildren(QPushButton)}
                    for forbidden in {"Save", "Submit", "Finalize", "Consensus", "정답 확정"}:
                        self.assertNotIn(forbidden, review_button_texts)

                    review.next_btn.click()
                    self.assertIn("sample_002", review.sample_title.text())
                    self.assertIn("2 / 2 · sample_002", review.sample_counter.text())
                    self.assertEqual(review.reviewer_status_labels["evaluator_b"].text(), "Not completed")
                    self.assertEqual(review.comment_boxes["evaluator_b"].toPlainText(), "")
                    self.assertTrue(review.previous_btn.isEnabled())
                    self.assertFalse(review.next_btn.isEnabled())
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.warning = original_warning

    def test_admin_sample_comparison_uses_dynamic_approved_reviewer_roster(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_warning = QMessageBox.warning
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.warning = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])
                    store.create_evaluator_account("kim_eval", "pw-123456", "pw-123456")
                    store.delete_evaluator_account("evaluator_c")

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    # Rotate off the fixture default first: it is flagged
                    # must_change_password, which would open a blocking dialog.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)
                    window.login.account.setText("admin")
                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    admin = window.stack.currentWidget()
                    review_button = [
                        button
                        for button in admin.findChildren(QPushButton)
                        if button.text() == "Review"
                    ][0]
                    review_button.click()
                    review = window.stack.currentWidget()

                    self.assertEqual(
                        list(review.reviewer_status_labels),
                        ["evaluator_a", "evaluator_b", "kim_eval"],
                    )
                    self.assertEqual(len(review.reviewer_columns), 3)
                    self.assertNotIn("evaluator_c", review.reviewer_status_labels)
                    self.assertEqual(
                        sorted(reviewer_id for _, reviewer_id in review.completeness_cells),
                        sorted(["evaluator_a", "evaluator_b", "kim_eval"] * 4),
                    )
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.warning = original_warning
            QMessageBox.question = original_question


    def test_login_has_no_prefill_inline_validation_and_password_toggle(self):
        try:
            from PySide6.QtWidgets import QApplication, QLineEdit, QMainWindow
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]

                    self.assertEqual(window.login.account.text(), "")
                    self.assertEqual(window.login.password.text(), "")
                    self.assertFalse(window.login.error_label.isVisible())

                    window.login.try_login()
                    self.assertTrue(window.login.error_label.isVisible())
                    self.assertEqual(
                        window.login.error_label.text(),
                        "Account and password are required.",
                    )

                    self.assertEqual(window.login.password.echoMode(), QLineEdit.Password)
                    window.login.show_password.setChecked(True)
                    self.assertEqual(window.login.password.echoMode(), QLineEdit.Normal)
                    window.login.show_password.setChecked(False)
                    self.assertEqual(window.login.password.echoMode(), QLineEdit.Password)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec

    def test_evaluator_save_uses_verified_wrapper_not_raw_save(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        verified_calls = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])

                    def raw_save_should_not_be_called(**payload):
                        raise AssertionError("raw save_evaluation was called")

                    def verified_save(**payload):
                        verified_calls.append(payload)
                        return {"evaluation_id": 1, **payload}

                    store.save_evaluation = raw_save_should_not_be_called
                    store.save_and_verify_evaluation = verified_save

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()

                    self.assertEqual(len(verified_calls), 1)
                    self.assertEqual(verified_calls[0]["evaluator_id"], "evaluator_a")
                    self.assertEqual(verified_calls[0]["sample_id"], "sample_001")
                    current_sample = evaluator.samples[evaluator.current_index]
                    self.assertIn(current_sample["sample_id"], evaluator.sample_title.text())
                    self.assertEqual(evaluator.comment.toPlainText(), "")
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info

    def test_save_failure_dumps_inputs_blocks_navigation_and_allows_logout_escape(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        criticals = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_critical = QMessageBox.critical
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.critical = lambda *args, **kwargs: (
            criticals.append(args[2] if len(args) > 2 else ""),
            QMessageBox.Ok,
        )[1]
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge"),
                            Sample("sample_002", "note 2", "course 2", "judge 2"),
                        ]
                    )

                    calls = []

                    def verified_save(**payload):
                        calls.append(payload)
                        raise RuntimeError("verification failed")

                    store.save_and_verify_evaluation = verified_save

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    evaluator.comment.setPlainText("preserve this input")
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.next_btn.click()

                    self.assertEqual(len(calls), 2)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertEqual(evaluator.comment.toPlainText(), "preserve this input")
                    self.assertTrue(criticals)
                    self.assertIsNotNone(evaluator._last_unsaved_dump_path)
                    self.assertTrue(evaluator._last_unsaved_dump_path.exists())
                    dumped = json.loads(evaluator._last_unsaved_dump_path.read_text())
                    self.assertEqual(dumped["detailed_comment"], "preserve this input")
                    self.assertTrue((store.db_path.parent / "save_failures.log").exists())
                    evaluator.sample_list.setCurrentRow(1)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertEqual(evaluator.comment.toPlainText(), "preserve this input")

                    evaluator.comment.setPlainText("changed after dump")
                    evaluator._escape_after_dump(window.show_login)
                    self.assertIs(window.stack.currentWidget(), window.login)
                    dumps = sorted(store.db_path.parent.glob("unsaved-evaluator_a-*.json"))
                    self.assertGreaterEqual(len(dumps), 2)
                    # Pick the latest dump the way recovery does -- by the
                    # dumped_at inside each file -- not by filename order, since
                    # the rolling autosave draft ("...-draft.json") sorts after
                    # the timestamped crash dumps but is not necessarily newest.
                    payloads = [json.loads(p.read_text()) for p in dumps]
                    latest_dump = max(payloads, key=lambda d: d.get("dumped_at", ""))
                    self.assertEqual(latest_dump["detailed_comment"], "changed after dump")
                    # The sample_id may be patient-linked: it belongs in the dump contents,
                    # never in the filename or the failure log.
                    for dump_path in dumps:
                        self.assertNotIn("sample_001", dump_path.name)
                    self.assertEqual(latest_dump["sample_id"], "sample_001")
                    log_path = store.db_path.parent / "save_failures.log"
                    log_text = log_path.read_text(encoding="utf-8")
                    self.assertNotIn("sample_001", log_text)
                    # The log still has to be usable: the digest in the line must
                    # match the digest in the filename it points at.
                    self.assertIn(pseudonymize_id("sample_001"), log_text)

                    if os.name == "posix":
                        # The dump holds the full sample_id and the clinician's
                        # free text, so it must not sit at default umask beside a
                        # 0600 database.
                        self.assertEqual(
                            stat.S_IMODE(log_path.stat().st_mode), 0o600
                        )
                        for dump_path in dumps:
                            self.assertEqual(
                                stat.S_IMODE(dump_path.stat().st_mode), 0o600
                            )
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.critical = original_critical
            QMessageBox.question = original_question
    def test_evaluator_end_advances_and_revisits_show_saved_scores(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *args, **kwargs: QMessageBox.Ok
        QMessageBox.question = lambda *args, **kwargs: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [Sample(f"sample_{i:03d}", f"note {i}", f"course {i}", f"judge {i}") for i in range(1, 5)]
                    )

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    def score_and_end(comp_key, overall_index, comment, false_info):
                        evaluator.start_pause_btn.click()
                        for key, check in evaluator.metric_checks["completeness"]:
                            check.setChecked(key == comp_key)
                        evaluator.correctness_counts["false_information"].setValue(false_info)
                        evaluator.overall.setCurrentIndex(overall_index)
                        evaluator.comment.setPlainText(comment)
                        evaluator.end_btn.click()

                    first_sample = evaluator.samples[0]["sample_id"]
                    second_sample = evaluator.samples[1]["sample_id"]

                    score_and_end("missing_reason", 3, "first review", 2)
                    self.assertEqual(evaluator.current_index, 1)
                    score_and_end("missing_condition", 4, "second review", 1)
                    self.assertEqual(evaluator.current_index, 2)

                    # Revisit the first completed sample: own scores must be shown.
                    evaluator.sample_list.setCurrentRow(0)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertIn(first_sample, evaluator.sample_title.text())
                    checked = {
                        key for key, check in evaluator.metric_checks["completeness"] if check.isChecked()
                    }
                    self.assertEqual(checked, {"missing_reason"})
                    self.assertEqual(evaluator.correctness_counts["false_information"].value(), 2)
                    self.assertEqual(evaluator.overall.currentData(), 3)
                    self.assertEqual(evaluator.comment.toPlainText(), "first review")
                    self.assertFalse(evaluator.comment.isEnabled())

                    # Revisit the second completed sample.
                    evaluator.sample_list.setCurrentRow(1)
                    self.assertIn(second_sample, evaluator.sample_title.text())
                    checked = {
                        key for key, check in evaluator.metric_checks["completeness"] if check.isChecked()
                    }
                    self.assertEqual(checked, {"missing_condition"})
                    self.assertEqual(evaluator.comment.toPlainText(), "second review")

                    # An un-evaluated sample stays blank.
                    evaluator.sample_list.setCurrentRow(3)
                    self.assertFalse(
                        any(check.isChecked() for _, check in evaluator.metric_checks["completeness"])
                    )
                    self.assertEqual(evaluator.comment.toPlainText(), "")
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_export_failure_shows_error_and_no_success_message(self):
        try:
            from PySide6.QtWidgets import (
                QApplication,
                QFileDialog,
                QMainWindow,
                QMessageBox,
                QPushButton,
            )
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        import doctor_eval_app.ui as ui_module
        from doctor_eval_app.ui import run_app

        captured_windows = []
        infos = []
        criticals = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_critical = QMessageBox.critical
        original_dialog = QFileDialog.getSaveFileName
        original_write_csv = ui_module.write_csv

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *a, **k: infos.append(a) or QMessageBox.Ok
        QMessageBox.critical = lambda *a, **k: criticals.append(a) or QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    target = str(Path(tmp) / "out.csv")
                    QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (target, "csv"))

                    def boom(rows, path):
                        raise OSError("disk full")

                    ui_module.write_csv = boom

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    # Rotate off the fixture default first: it is flagged
                    # must_change_password, which would open a blocking dialog.
                    store.set_password("admin", ADMIN_TEST_PASSWORD)
                    window.login.account.setText("admin")
                    window.login.password.setText(ADMIN_TEST_PASSWORD)
                    window.login.try_login()
                    admin = window.stack.currentWidget()
                    export_csv = [
                        button
                        for button in admin.findChildren(QPushButton)
                        if button.text() == "Export CSV"
                    ][0]
                    export_csv.click()

                    self.assertEqual(len(criticals), 1)
                    self.assertEqual(len(infos), 0)
                    self.assertFalse(Path(target).exists())
                finally:
                    ui_module.write_csv = original_write_csv
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.critical = original_critical
            QFileDialog.getSaveFileName = original_dialog

    def test_end_requires_overall_quality_before_saving(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        warnings = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_warning = QMessageBox.warning

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *a, **k: QMessageBox.Ok
        QMessageBox.warning = lambda *a, **k: warnings.append(a) or QMessageBox.Ok
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # End without Overall Quality must be blocked: warned, not saved.
                    evaluator.start_pause_btn.click()
                    evaluator.end_btn.click()
                    self.assertEqual(len(warnings), 1)
                    store.set_actor(None)  # inspect as the harness, not as the logged-in user
                    self.assertEqual(store.export_rows(), [])
                    self.assertIsNotNone(evaluator.started_at)

                    # Selecting Overall Quality unblocks the save.
                    evaluator.overall.setCurrentIndex(3)
                    evaluator.end_btn.click()
                    store.set_actor(None)  # inspect as the harness, not as the logged-in user
                    rows = store.export_rows()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["overall_quality"], 3)
                    self.assertEqual(rows[0]["completed"], 1)
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.warning = original_warning

    def test_revisiting_completed_sample_shows_recorded_time(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *a, **k: QMessageBox.Ok
        QMessageBox.question = lambda *a, **k: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                clock = FakeClock()
                try:
                    store.import_samples(
                        [Sample(f"sample_{i:03d}", f"n{i}", f"c{i}", f"j{i}") for i in range(1, 4)]
                    )

                    self.assertEqual(run_app(store, clock=clock), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    evaluator.start_pause_btn.click()
                    clock.advance(125)
                    evaluator.tick()
                    self.assertEqual(evaluator.timer_display.text(), "02:05")
                    evaluator.overall.setCurrentIndex(3)
                    evaluator.end_btn.click()

                    # After END the next (incomplete) sample starts fresh.
                    self.assertEqual(evaluator.timer_display.text(), "00:00")

                    # Revisiting the completed sample shows the recorded time.
                    evaluator.sample_list.setCurrentRow(0)
                    self.assertEqual(evaluator.current_index, 0)
                    self.assertEqual(evaluator.timer_display.text(), "02:05")

                    # An incomplete sample still shows a fresh timer.
                    evaluator.sample_list.setCurrentRow(2)
                    self.assertEqual(evaluator.timer_display.text(), "00:00")
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_login_recovers_unsaved_dump_into_form_and_cleans_up_on_save(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_info = QMessageBox.information
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.information = lambda *a, **k: QMessageBox.Ok
        QMessageBox.question = lambda *a, **k: QMessageBox.Yes
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples(
                        [
                            Sample("sample_001", "note", "course", "judge"),
                            Sample("sample_002", "n2", "c2", "j2"),
                        ]
                    )
                    # Worst case: dumped with overall_quality unset.
                    dump = {
                        "evaluator_id": "evaluator_a",
                        "sample_id": "sample_001",
                        "display_order": 1,
                        "scores": {
                            "completeness": 4,
                            "correctness": 5,
                            "readability": 5,
                            "overall_quality": None,
                        },
                        "deductions": {
                            "completeness": ["missing_condition"],
                            "readability": [],
                            "correctness": {"false_information": 0, "causal_error": 0},
                        },
                        "detailed_comment": "recovered text",
                        "timer": {
                            "active_seconds": 24,
                            "paused_seconds": 0,
                            "pause_count": 0,
                            "reset_count": 0,
                        },
                        "llm_judge_view": {"open_count": 1, "visible_seconds": 25},
                        "dumped_at": "2026-06-17T13:21:05+09:00",
                    }
                    dump_path = (
                        store.db_path.parent
                        / "unsaved-evaluator_a-sample_001-20260617-132105.json"
                    )
                    dump_path.write_text(json.dumps(dump), encoding="utf-8")

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    # Dump re-entered into the form, paused until the evaluator
                    # presses Resume (timer does not auto-start).
                    self.assertEqual(evaluator.comment.toPlainText(), "recovered text")
                    checked = {
                        key
                        for key, check in evaluator.metric_checks["completeness"]
                        if check.isChecked()
                    }
                    self.assertEqual(checked, {"missing_condition"})
                    self.assertIsNotNone(evaluator.started_at)
                    self.assertEqual(evaluator.elapsed_seconds, 24)
                    self.assertTrue(evaluator.comment.isEnabled())
                    self.assertIn("sample_001", evaluator.sample_title.text())
                    self.assertTrue(dump_path.exists())
                    # Paused: timer stopped and the button invites Resume.
                    self.assertFalse(evaluator.timer.isActive())
                    self.assertIsNone(evaluator.active_started_at)
                    self.assertEqual(evaluator.start_pause_btn.text(), "Resume")

                    # Pressing Resume starts timing from the restored elapsed time.
                    evaluator.start_pause_btn.click()
                    self.assertTrue(evaluator.timer.isActive())
                    self.assertEqual(evaluator.start_pause_btn.text(), "Pause")
                    self.assertEqual(evaluator.elapsed_seconds, 24)

                    # Finishing the evaluation saves it and removes the dump.
                    evaluator.overall.setCurrentIndex(5)
                    evaluator.end_btn.click()
                    store.set_actor(None)  # inspect as the harness, not as the logged-in user
                    rows = store.export_rows()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["overall_quality"], 5)
                    self.assertEqual(rows[0]["completed"], 1)
                    self.assertFalse(dump_path.exists())
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.information = original_info
            QMessageBox.question = original_question

    def test_declining_recovery_leaves_form_blank_and_keeps_dump(self):
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        except ModuleNotFoundError:
            self.skipTest("PySide6 is not installed")

        from doctor_eval_app.ui import run_app

        captured_windows = []
        original_show = QMainWindow.show
        original_exec = QApplication.exec
        original_question = QMessageBox.question

        def capture_show(window):
            captured_windows.append(window)
            return original_show(window)

        QMainWindow.show = capture_show
        QApplication.exec = lambda self: 0
        QMessageBox.question = lambda *a, **k: QMessageBox.No
        try:
            with tempfile.TemporaryDirectory() as tmp:
                store = create_store(Path(tmp))
                try:
                    store.import_samples([Sample("sample_001", "note", "course", "judge")])
                    dump = {
                        "evaluator_id": "evaluator_a",
                        "sample_id": "sample_001",
                        "display_order": 1,
                        "scores": {"overall_quality": None},
                        "deductions": {"completeness": ["missing_condition"]},
                        "detailed_comment": "recovered text",
                        "timer": {},
                        "llm_judge_view": {},
                        "dumped_at": "2026-06-17T13:21:05+09:00",
                    }
                    dump_path = store.db_path.parent / "unsaved-evaluator_a-sample_001-x.json"
                    dump_path.write_text(json.dumps(dump), encoding="utf-8")

                    self.assertEqual(run_app(store), 0)
                    window = captured_windows[-1]
                    window.login.account.setText("evaluator_a")
                    window.login.password.setText("pass1234a")
                    window.login.try_login()
                    evaluator = window.stack.currentWidget()

                    self.assertEqual(evaluator.comment.toPlainText(), "")
                    self.assertIsNone(evaluator.started_at)
                    self.assertEqual(evaluator._recovery_dumps, {})
                    self.assertTrue(dump_path.exists())
                finally:
                    store.close()
        finally:
            QMainWindow.show = original_show
            QApplication.exec = original_exec
            QMessageBox.question = original_question


if __name__ == "__main__":
    unittest.main()
