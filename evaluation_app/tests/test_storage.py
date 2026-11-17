import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app.models import Sample
from doctor_eval_app.storage import USER_ID_PATTERN, DoctorEvalStore, EvaluationSaveError


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_initialize_creates_admin_and_three_evaluator_users(self):
        evaluator_a = self.store.authenticate("evaluator_a", "pass1234a")
        evaluator_b = self.store.authenticate("evaluator_b", "pass1234b")
        evaluator_c = self.store.authenticate("evaluator_c", "pass1234c")
        admin = self.store.authenticate("admin", "admin")

        self.assertEqual(evaluator_a.role, "evaluator")
        self.assertEqual(evaluator_b.role, "evaluator")
        self.assertEqual(evaluator_c.role, "evaluator")
        self.assertEqual(admin.role, "admin")
        self.assertIsNone(self.store.authenticate("evaluator_a", "wrong"))

    def test_initialize_migrates_existing_user_table_for_account_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                create table users (
                  user_id text primary key,
                  role text not null,
                  password_salt text not null,
                  password_hash text not null
                );
                """
            )
            conn.close()

            store = DoctorEvalStore(db_path)
            try:
                store.initialize()
                user = store.authenticate("evaluator_a", "pass1234a")
            finally:
                store.close()

        self.assertIsNotNone(user)
        self.assertEqual(user.status, "approved")

    def test_list_users_can_filter_evaluators(self):
        evaluators = self.store.list_users(role="evaluator")

        self.assertEqual(
            [user.user_id for user in evaluators],
            ["evaluator_a", "evaluator_b", "evaluator_c"],
        )

    def _fresh_store_without_accounts(self):
        """A brand-new database exactly as the shipped app creates it: no accounts.

        ``_accounts`` seeds fixture accounts into every new test database, so
        first-run behaviour needs the seed switched off on this instance.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = DoctorEvalStore(Path(tmp.name) / "fresh.db")
        store.seed_accounts = ()
        store.initialize()
        self.addCleanup(store.close)
        return store

    def test_shipped_store_seeds_no_accounts(self):
        store = self._fresh_store_without_accounts()

        self.assertFalse(store.has_admin())
        self.assertEqual(store.list_users(), [])
        self.assertIsNone(store.authenticate("admin", "admin"))

    def test_create_initial_admin_only_while_no_admin_exists(self):
        store = self._fresh_store_without_accounts()

        admin = store.create_initial_admin(
            "lab_admin", "s3cret-pass", "s3cret-pass", name="Lab Admin"
        )

        self.assertEqual((admin.user_id, admin.role), ("lab_admin", "admin"))
        self.assertTrue(store.has_admin())
        logged_in = store.authenticate("lab_admin", "s3cret-pass")
        self.assertEqual(logged_in.role, "admin")
        self.assertEqual(logged_in.name, "Lab Admin")
        # The chosen password is the admin's own, so no forced rotation.
        self.assertFalse(store.password_change_required("lab_admin"))
        with self.assertRaises(PermissionError):
            store.create_initial_admin("second", "another-pass", "another-pass")
        self.assertIsNone(store.authenticate("second", "another-pass"))

    def test_create_initial_admin_validates_input(self):
        store = self._fresh_store_without_accounts()

        for user_id, password, check in (
            ("", "longenough", "longenough"),
            ("   ", "longenough", "longenough"),
            ("ab", "longenough", "longenough"),
            ("bad id", "longenough", "longenough"),
            ("=cmd|x", "longenough", "longenough"),
            ("_leading", "longenough", "longenough"),
            ("admin", "short", "short"),
            ("admin", "longenough", "different1"),
        ):
            with self.subTest(user_id=user_id, password=password):
                with self.assertRaises(ValueError):
                    store.create_initial_admin(user_id, password, check)
        self.assertFalse(store.has_admin())

    def test_admin_creates_evaluator_that_can_log_in_immediately(self):
        admin = self.store.authenticate("admin", "admin")
        self.store.set_actor(admin)

        created = self.store.create_evaluator_account(
            "kim_eval", "pw-123456", "pw-123456", name="Kim Evaluator"
        )

        self.assertEqual((created.user_id, created.role), ("kim_eval", "evaluator"))
        user = self.store.authenticate("kim_eval", "pw-123456")
        self.assertIsNotNone(user)
        self.assertEqual(user.role, "evaluator")
        self.assertEqual(user.name, "Kim Evaluator")
        self.assertEqual(user.status, "approved")
        self.assertIn(
            "kim_eval", [u.user_id for u in self.store.list_users(role="evaluator")]
        )

    def test_account_ids_are_unique_case_insensitively(self):
        self.store.create_evaluator_account("Kim_eval", "pw-123456", "pw-123456")

        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("kim_EVAL", "pw-123456", "pw-123456")
        with self.assertRaises(ValueError):
            self.store.create_evaluator_account("EVALUATOR_A", "pw-123456", "pw-123456")

    def test_account_id_cannot_end_in_a_character_file_names_strip(self):
        # The pattern itself must not accept a trailing newline ("$" would).
        self.assertIsNone(USER_ID_PATTERN.fullmatch("kim\n"))
        for user_id in ("kim.", "kim-", "kim..", "kim.-"):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    self.store.create_evaluator_account(
                        user_id, "pw-123456", "pw-123456"
                    )
        for user_id in ("kim", "kim_", "k.im", "dr-kim", "a1b"):
            with self.subTest(user_id=user_id):
                self.store.create_evaluator_account(user_id, "pw-123456", "pw-123456")

    def test_evaluator_name_defaults_to_id(self):
        self.store.create_evaluator_account("lee_eval", "pw-123456", "pw-123456")

        self.assertEqual(self.store.authenticate("lee_eval", "pw-123456").name, "lee_eval")

    def test_create_evaluator_rejects_duplicate_id_and_bad_input(self):
        for user_id, password, check in (
            ("evaluator_a", "pw-123456", "pw-123456"),
            ("admin", "pw-123456", "pw-123456"),
            ("new_eval", "pw-123456", "different"),
            ("new_eval", "short", "short"),
            ("", "pw-123456", "pw-123456"),
            ("bad/id", "pw-123456", "pw-123456"),
        ):
            with self.subTest(user_id=user_id):
                with self.assertRaises(ValueError):
                    self.store.create_evaluator_account(user_id, password, check)
        self.assertIsNone(self.store.authenticate("new_eval", "pw-123456"))

    def test_evaluator_session_cannot_create_accounts(self):
        self.store.set_actor(self.store.authenticate("evaluator_a", "pass1234a"))

        with self.assertRaises(PermissionError):
            self.store.create_evaluator_account("sneaky", "pw-123456", "pw-123456")
        with self.assertRaises(PermissionError):
            self.store.create_initial_admin("sneaky", "pw-123456", "pw-123456")
        self.store.set_actor(None)
        self.assertIsNone(self.store.authenticate("sneaky", "pw-123456"))

    def test_admin_can_reset_an_evaluator_password(self):
        self.store.set_actor(self.store.authenticate("admin", "admin"))

        self.assertTrue(self.store.set_password("evaluator_a", "fresh-pass-1"))

        self.assertIsNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertIsNotNone(self.store.authenticate("evaluator_a", "fresh-pass-1"))

    def test_legacy_pending_or_rejected_rows_cannot_log_in_or_appear_in_progress(self):
        """Databases from the old self-signup flow may still hold such rows."""
        self.store.import_samples([Sample("sample_001", "note", "course", "")])
        self.store.create_evaluator_account("pending_eval", "pw-123456", "pw-123456")
        self.store.create_evaluator_account("rejected_eval", "pw-123456", "pw-123456")
        with self.store.conn:
            self.store.conn.execute(
                "update users set status = 'pending' where user_id = 'pending_eval'"
            )
            self.store.conn.execute(
                "update users set status = 'rejected' where user_id = 'rejected_eval'"
            )

        for user_id, status in (("pending_eval", "pending"), ("rejected_eval", "rejected")):
            with self.subTest(user_id=user_id):
                self.assertIsNone(self.store.authenticate(user_id, "pw-123456"))
                self.assertEqual(self.store.login_block_reason(user_id), status)
                self.assertNotIn(
                    user_id,
                    [row["evaluator_id"] for row in self.store.evaluator_progress()],
                )

    def test_delete_evaluator_account_removes_user_and_evaluations(self):
        self.store.import_samples(
            [Sample("sample_001", "note 1", "course 1", "judge 1")]
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 4,
                "readability": 3,
                "overall_quality": 4,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="done",
            timer={},
            llm_judge_view={},
        )

        self.store.delete_evaluator_account("evaluator_a")

        self.assertIsNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertEqual(
            [user.user_id for user in self.store.list_users(role="evaluator")],
            ["evaluator_b", "evaluator_c"],
        )
        self.assertEqual(self.store.export_rows(), [])
        self.assertEqual(
            [row["evaluator_id"] for row in self.store.evaluator_progress()],
            ["evaluator_b", "evaluator_c"],
        )

    def test_delete_evaluator_account_refuses_admin_and_missing_users(self):
        with self.assertRaises(ValueError):
            self.store.delete_evaluator_account("admin")

        with self.assertRaises(ValueError):
            self.store.delete_evaluator_account("missing_eval")

    def test_import_samples_rejects_duplicate_ids(self):
        samples = [
            Sample("sample_001", "note a", "course a", "judge a"),
            Sample("sample_001", "note b", "course b", "judge b"),
        ]

        with self.assertRaises(ValueError):
            self.store.import_samples(samples)

    def test_list_samples_for_evaluator_uses_deterministic_order(self):
        self.store.import_samples(
            [
                Sample("sample_001", "note 1", "course 1", "judge 1"),
                Sample("sample_002", "note 2", "course 2", "judge 2"),
                Sample("sample_003", "note 3", "course 3", "judge 3"),
            ]
        )

        first = self.store.list_samples_for_evaluator("evaluator_a")
        second = self.store.list_samples_for_evaluator("evaluator_a")

        self.assertEqual(
            [row["sample_id"] for row in first],
            [row["sample_id"] for row in second],
        )
        self.assertEqual([row["display_order"] for row in first], [1, 2, 3])

    # --- batched top-up rounds -------------------------------------------
    #
    # The whole point of samples.batch: a later round must never disturb the
    # presentation order of a round evaluators have already worked through.
    # Before batching, list_samples_for_evaluator shuffled the entire table, so
    # importing one new sample renumbered and re-interleaved every completed one.

    def _save_minimal_evaluation(self, evaluator_id, sample_id, display_order):
        self.store.save_evaluation(
            evaluator_id=evaluator_id,
            sample_id=sample_id,
            display_order=display_order,
            scores={
                "completeness": 4,
                "correctness": 1,
                "readability": 3,
                "overall_quality": 4,
            },
            deductions={"completeness": [], "correctness": {}, "readability": []},
            detailed_comment="",
            timer={
                "started_at": "2026-06-08T09:00:00+09:00",
                "ended_at": "2026-06-08T09:01:00+09:00",
                "active_seconds": 60,
            },
            llm_judge_view={"opened": False},
        )

    def _import_round(self, batch, count, prefix):
        self.store.import_samples(
            [
                Sample(f"{prefix}_{i:03d}", f"note {i}", f"course {i}", f"judge {i}")
                for i in range(1, count + 1)
            ],
            batch=batch,
        )

    def test_later_batch_is_appended_without_moving_the_first_batch(self):
        self._import_round(1, 100, "base")
        before = self.store.list_samples_for_evaluator("evaluator_a")

        self._import_round(2, 30, "top")
        after = self.store.list_samples_for_evaluator("evaluator_a")

        self.assertEqual(len(after), 130)
        # Every one of the original 100 keeps its exact sample_id *and* number.
        self.assertEqual(
            [(r["sample_id"], r["display_order"]) for r in before],
            [(r["sample_id"], r["display_order"]) for r in after[:100]],
        )
        # The new round occupies 101..130 and nothing else.
        self.assertEqual([r["display_order"] for r in after[100:]], list(range(101, 131)))
        self.assertTrue(all(r["batch"] == 2 for r in after[100:]))

    def test_first_batch_order_does_not_depend_on_the_batch_column(self):
        """Batch 1's seed must stay the bare evaluator_id.

        Evaluations already collected on the pre-batch build were ordered by
        ``random.Random(evaluator_id)`` over the samples sorted by sample_id.
        Reproducing that exactly is what makes an in-place upgrade safe, so pin
        it here rather than letting a seed refactor quietly renumber real data.
        """
        import random

        self._import_round(1, 100, "base")
        self._import_round(2, 30, "top")

        expected = sorted(f"base_{i:03d}" for i in range(1, 101))
        random.Random("evaluator_a").shuffle(expected)

        rows = self.store.list_samples_for_evaluator("evaluator_a")
        self.assertEqual([r["sample_id"] for r in rows[:100]], expected)

    def test_later_batch_is_shuffled_and_differs_per_evaluator(self):
        self._import_round(1, 10, "base")
        self._import_round(2, 30, "top")

        def tail(evaluator_id):
            return [
                r["sample_id"]
                for r in self.store.list_samples_for_evaluator(evaluator_id)[10:]
            ]

        for_a, for_b = tail("evaluator_a"), tail("evaluator_b")
        self.assertEqual(sorted(for_a), sorted(for_b))
        self.assertNotEqual(for_a, sorted(for_a))  # actually shuffled
        self.assertNotEqual(for_a, for_b)  # and independently per evaluator
        self.assertEqual(for_a, tail("evaluator_a"))  # but stable across reads

    def test_completed_first_batch_evaluations_keep_their_saved_display_order(self):
        self._import_round(1, 10, "base")
        rows = self.store.list_samples_for_evaluator("evaluator_a")
        for row in rows:
            self._save_minimal_evaluation(
                "evaluator_a", row["sample_id"], row["display_order"]
            )

        self._import_round(2, 30, "top")

        listed = {
            r["sample_id"]: r["display_order"]
            for r in self.store.list_samples_for_evaluator("evaluator_a")
        }
        saved = {
            r["sample_id"]: r["display_order"]
            for r in self.store.export_rows("evaluator_a")
        }
        # The saved numbers still describe where the sample sits in the list.
        self.assertEqual({k: listed[k] for k in saved}, saved)

    def test_import_refuses_a_sample_id_that_already_exists(self):
        self._import_round(1, 5, "base")

        with self.assertRaises(ValueError) as ctx:
            self.store.import_samples(
                [Sample("base_003", "dup", "dup", "dup")], batch=2
            )
        self.assertIn("base_003", str(ctx.exception))
        self.assertEqual(len(self.store.list_samples_for_evaluator("evaluator_a")), 5)

    def test_batch_has_evaluations_reports_per_batch(self):
        self._import_round(1, 3, "base")
        self._import_round(2, 3, "top")
        self.assertFalse(self.store.batch_has_evaluations(1))
        self.assertFalse(self.store.batch_has_evaluations(2))

        self._save_minimal_evaluation("evaluator_a", "base_001", 1)

        self.assertTrue(self.store.batch_has_evaluations(1))
        self.assertFalse(self.store.batch_has_evaluations(2))

    def test_sample_batches_groups_ids_by_round(self):
        self._import_round(1, 2, "base")
        self._import_round(2, 3, "top")
        self.assertEqual(
            self.store.sample_batches(),
            {1: ["base_001", "base_002"], 2: ["top_001", "top_002", "top_003"]},
        )

    def test_initialize_backfills_batch_one_on_a_pre_batch_database(self):
        """A database from the shipped build has no batch column at all."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                create table samples (
                  sample_id text primary key,
                  discharge_note text not null,
                  brief_hospital_course text not null,
                  llm_judge text not null default '',
                  metadata_json text not null default '{}'
                );
                insert into samples (sample_id, discharge_note, brief_hospital_course)
                values ('old_001', 'note', 'course'), ('old_002', 'note', 'course');
                """
            )
            conn.commit()
            conn.close()

            store = DoctorEvalStore(db_path)
            store.initialize()
            try:
                self.assertEqual(store.sample_batches(), {1: ["old_001", "old_002"]})
            finally:
                store.close()

    def test_save_evaluation_and_export_rows_exclude_raw_text(self):
        self.store.import_samples(
            [Sample("sample_001", "private note", "private course", "private judge")]
        )

        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 4,
                "correctness": 1,
                "readability": 3,
                "overall_quality": 4,
            },
            deductions={
                "completeness": ["missing_condition"],
                "correctness": {"false_information": 5, "causal_error": 1},
                "readability": ["duplicated", "formatting"],
            },
            detailed_comment="usable with edits",
            timer={
                "started_at": "2026-06-08T09:00:00+09:00",
                "ended_at": "2026-06-08T09:03:00+09:00",
                "active_seconds": 180,
                "paused_seconds": 0,
                "pause_count": 0,
                "reset_count": 0,
            },
            llm_judge_view={
                "opened": True,
                "first_opened_at": "2026-06-08T09:01:00+09:00",
                "open_count": 1,
                "visible_seconds": 40,
            },
        )

        rows = self.store.export_rows()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["sample_id"], "sample_001")
        self.assertEqual(row["completed"], 1)
        self.assertEqual(row["correctness_score"], 1)
        self.assertEqual(row["readability_score"], 3)
        self.assertEqual(row["correctness_false_information_count"], 5)
        self.assertEqual(row["correctness_causal_error_count"], 1)
        self.assertEqual(row["detailed_comment"], "usable with edits")
        self.assertEqual(row["llm_judge_opened"], 1)
        self.assertNotIn("discharge_note", row)
        self.assertNotIn("brief_hospital_course", row)
        self.assertNotIn("llm_judge", row)


    def test_get_evaluation_roundtrip_includes_normalized_storage_fields(self):
        self.store.import_samples(
            [Sample("sample_001", "private note", "private course", "private judge")]
        )

        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=7,
            scores={
                "completeness": 4,
                "correctness": 2,
                "readability": 5,
                "overall_quality": 3,
            },
            deductions={
                "completeness": ["missing_condition"],
                "correctness": ["false_information", "false_information"],
                "readability": ["duplicated"],
            },
            detailed_comment="checked after save",
            timer={
                "started_at": "2026-06-08T09:00:00+09:00",
                "ended_at": "2026-06-08T09:04:00+09:00",
                "active_seconds": 210,
                "paused_seconds": 30,
                "pause_count": 2,
                "reset_count": 1,
            },
            llm_judge_view={
                "opened": True,
                "first_opened_at": "2026-06-08T09:02:00+09:00",
                "open_count": 3,
                "visible_seconds": 75,
            },
        )

        row = self.store.get_evaluation("evaluator_a", "sample_001")

        self.assertEqual(row["completed"], 1)
        self.assertEqual(
            row["scores"],
            {
                "completeness": 4,
                "correctness": 2,
                "readability": 5,
                "overall_quality": 3,
            },
        )
        self.assertEqual(row["overall_quality"], 3)
        self.assertEqual(
            json.loads(row["deductions_json"]),
            {
                "completeness": ["missing_condition"],
                "correctness": {"false_information": 2, "causal_error": 0},
                "readability": ["duplicated"],
            },
        )
        self.assertEqual(row["detailed_comment"], "checked after save")
        self.assertEqual(row["started_at"], "2026-06-08T09:00:00+09:00")
        self.assertEqual(row["ended_at"], "2026-06-08T09:04:00+09:00")
        self.assertEqual(row["active_seconds"], 210)
        self.assertEqual(row["paused_seconds"], 30)
        self.assertEqual(row["pause_count"], 2)
        self.assertEqual(row["reset_count"], 1)
        self.assertEqual(row["llm_judge_opened"], 1)
        self.assertEqual(
            row["llm_judge_first_opened_at"], "2026-06-08T09:02:00+09:00"
        )
        self.assertEqual(row["llm_judge_open_count"], 3)
        self.assertEqual(row["llm_judge_visible_seconds"], 75)
        self.assertIsNone(self.store.get_evaluation("evaluator_a", "missing_sample"))

    def test_save_and_verify_evaluation_returns_verified_row_on_success(self):
        self.store.import_samples(
            [Sample("sample_001", "private note", "private course", "private judge")]
        )

        row = self.store.save_and_verify_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 4,
                "readability": 3,
                "overall_quality": 4,
            },
            deductions={
                "completeness": [],
                "correctness": {"false_information": 1, "causal_error": 0},
                "readability": [],
            },
            detailed_comment="verified",
            timer={"active_seconds": 12},
            llm_judge_view={"opened": False},
        )

        self.assertEqual(row["completed"], 1)
        self.assertEqual(row["scores"]["correctness"], 4)
        self.assertEqual(row["active_seconds"], 12)
        self.assertEqual(
            json.loads(row["deductions_json"])["correctness"],
            {"false_information": 1, "causal_error": 0},
        )

    def test_save_and_verify_evaluation_raises_on_first_mismatch(self):
        self.store.import_samples(
            [Sample("sample_001", "private note", "private course", "private judge")]
        )
        original_save = self.store.save_evaluation

        def mismatching_save(**kwargs):
            original_save(**kwargs)
            with self.store.conn:
                self.store.conn.execute(
                    """
                    update evaluations
                       set detailed_comment = 'corrupted'
                     where evaluator_id = ?
                       and sample_id = ?
                    """,
                    (kwargs["evaluator_id"], kwargs["sample_id"]),
                )

        self.store.save_evaluation = mismatching_save

        with self.assertRaises(EvaluationSaveError) as error:
            self.store.save_and_verify_evaluation(
                evaluator_id="evaluator_a",
                sample_id="sample_001",
                display_order=1,
                scores={
                    "completeness": 5,
                    "correctness": 4,
                    "readability": 3,
                    "overall_quality": 4,
                },
                deductions={"completeness": [], "correctness": [], "readability": []},
                detailed_comment="expected",
                timer={},
                llm_judge_view={},
            )

        message = str(error.exception)
        self.assertIn("evaluator_id='evaluator_a'", message)
        self.assertIn("sample_id='sample_001'", message)
        self.assertIn("detailed_comment", message)
        self.assertIn("'expected'", message)
        self.assertIn("'corrupted'", message)

    def test_save_and_verify_evaluation_raises_on_missing_row_after_no_op_write(self):
        self.store.import_samples(
            [Sample("sample_001", "private note", "private course", "private judge")]
        )

        def no_op_save(**kwargs):
            return None

        self.store.save_evaluation = no_op_save

        with self.assertRaises(EvaluationSaveError) as error:
            self.store.save_and_verify_evaluation(
                evaluator_id="evaluator_a",
                sample_id="sample_001",
                display_order=1,
                scores={
                    "completeness": 5,
                    "correctness": 4,
                    "readability": 3,
                    "overall_quality": 4,
                },
                deductions={"completeness": [], "correctness": [], "readability": []},
                detailed_comment="expected",
                timer={},
                llm_judge_view={},
            )

        message = str(error.exception)
        self.assertIn("evaluator_id='evaluator_a'", message)
        self.assertIn("sample_id='sample_001'", message)
        self.assertIn("row missing", message)

    def test_completed_evaluation_details_include_source_text_read_only_payload(self):
        self.store.import_samples(
            [
                Sample("sample_001", "private note 1", "private course 1", "judge 1"),
                Sample("sample_002", "private note 2", "private course 2", "judge 2"),
            ]
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=2,
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
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_002",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 5,
                "readability": 5,
                "overall_quality": 5,
            },
            deductions={"completeness": [], "correctness": {}, "readability": []},
            detailed_comment="draft",
            timer={},
            llm_judge_view={},
            completed=False,
        )

        details = self.store.completed_evaluation_details("evaluator_a")

        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["evaluator_id"], "evaluator_a")
        self.assertEqual(detail["sample_id"], "sample_001")
        self.assertEqual(detail["display_order"], 2)
        self.assertEqual(detail["discharge_note"], "private note 1")
        self.assertEqual(detail["brief_hospital_course"], "private course 1")
        self.assertEqual(detail["llm_judge"], "judge 1")
        self.assertEqual(
            detail["scores"],
            {
                "completeness": 4,
                "correctness": 2,
                "readability": 5,
                "overall_quality": 3,
            },
        )
        self.assertEqual(
            detail["deductions"],
            {
                "completeness": ["missing_condition"],
                "correctness": {"false_information": 2, "causal_error": 1},
                "readability": [],
            },
        )
        self.assertEqual(detail["detailed_comment"], "needs source check")
        self.assertNotIn("discharge_note", self.store.export_rows()[0])

    def test_sample_review_comparison_details_returns_all_samples_and_reviewers(self):
        self.store.import_samples(
            [
                Sample("sample_001", "note 1", "course 1", "judge 1"),
                Sample("sample_002", "note 2", "course 2", "judge 2"),
            ]
        )
        self.store.save_evaluation(
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
        self.store.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="sample_001",
            display_order=1,
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

        details = self.store.sample_review_comparison_details()

        self.assertEqual([detail["sample_id"] for detail in details], ["sample_001", "sample_002"])
        first = details[0]
        self.assertEqual(first["discharge_note"], "note 1")
        self.assertEqual(first["brief_hospital_course"], "course 1")
        self.assertEqual(first["llm_judge"], "judge 1")
        self.assertEqual(first["sample_order"], 1)
        reviewer_rows = {row["evaluator_id"]: row for row in first["reviewers"]}
        self.assertEqual(list(reviewer_rows), ["evaluator_a", "evaluator_b", "evaluator_c"])
        self.assertTrue(reviewer_rows["evaluator_a"]["completed"])
        self.assertEqual(reviewer_rows["evaluator_a"]["scores"]["completeness"], 4)
        self.assertEqual(
            reviewer_rows["evaluator_a"]["deductions"],
            {
                "completeness": ["missing_condition"],
                "correctness": {"false_information": 2, "causal_error": 1},
                "readability": [],
            },
        )
        self.assertEqual(reviewer_rows["evaluator_a"]["detailed_comment"], "needs source check")
        self.assertFalse(reviewer_rows["evaluator_b"]["completed"])
        self.assertIsNone(reviewer_rows["evaluator_b"]["scores"])
        self.assertIsNone(reviewer_rows["evaluator_b"]["deductions"])
        self.assertEqual(reviewer_rows["evaluator_b"]["detailed_comment"], "")
        self.assertFalse(reviewer_rows["evaluator_c"]["completed"])
        second_reviewers = {row["evaluator_id"]: row for row in details[1]["reviewers"]}
        self.assertTrue(all(not row["completed"] for row in second_reviewers.values()))

    def test_sample_review_comparison_details_uses_approved_evaluator_roster(self):
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        for user_id in ("pending_eval", "rejected_eval", "kim_eval"):
            self.store.create_evaluator_account(user_id, "pw-123456", "pw-123456")
        # Leftover rows from the old self-signup flow stay off the roster.
        with self.store.conn:
            self.store.conn.execute(
                "update users set status = 'pending' where user_id = 'pending_eval'"
            )
            self.store.conn.execute(
                "update users set status = 'rejected' where user_id = 'rejected_eval'"
            )
        self.store.delete_evaluator_account("evaluator_c")

        details = self.store.sample_review_comparison_details()

        self.assertEqual(
            [row["evaluator_id"] for row in details[0]["reviewers"]],
            ["evaluator_a", "evaluator_b", "kim_eval"],
        )

    def test_evaluator_progress_reports_each_evaluator(self):
        self.store.import_samples(
            [
                Sample("sample_001", "note 1", "course 1", "judge 1"),
                Sample("sample_002", "note 2", "course 2", "judge 2"),
            ]
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 5,
                "readability": 5,
                "overall_quality": 5,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="",
            timer={},
            llm_judge_view={},
        )

        progress = self.store.evaluator_progress()

        self.assertEqual(
            progress,
            [
                {"evaluator_id": "evaluator_a", "completed": 0, "total": 2},
                {"evaluator_id": "evaluator_b", "completed": 1, "total": 2},
                {"evaluator_id": "evaluator_c", "completed": 0, "total": 2},
            ],
        )

    def test_evaluator_score_summary_reports_progress_and_average_scores(self):
        self.store.import_samples(
            [
                Sample("sample_001", "note 1", "course 1", "judge 1"),
                Sample("sample_002", "note 2", "course 2", "judge 2"),
            ]
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 4,
                "readability": 3,
                "overall_quality": 4,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="",
            timer={},
            llm_judge_view={},
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_002",
            display_order=2,
            scores={
                "completeness": 3,
                "correctness": 2,
                "readability": 5,
                "overall_quality": 2,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="",
            timer={},
            llm_judge_view={},
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 4,
                "correctness": 4,
                "readability": 4,
                "overall_quality": 4,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="",
            timer={},
            llm_judge_view={},
        )

        summary = self.store.evaluator_score_summary()

        self.assertEqual(
            summary,
            [
                {
                    "evaluator_id": "evaluator_a",
                    "completed": 2,
                    "total": 2,
                    "completeness_avg": 4.0,
                    "correctness_avg": 3.0,
                    "readability_avg": 4.0,
                    "overall_quality_avg": 3.0,
                },
                {
                    "evaluator_id": "evaluator_b",
                    "completed": 1,
                    "total": 2,
                    "completeness_avg": 4.0,
                    "correctness_avg": 4.0,
                    "readability_avg": 4.0,
                    "overall_quality_avg": 4.0,
                },
                {
                    "evaluator_id": "evaluator_c",
                    "completed": 0,
                    "total": 2,
                    "completeness_avg": None,
                    "correctness_avg": None,
                    "readability_avg": None,
                    "overall_quality_avg": None,
                },
            ],
        )

    def test_delete_evaluation_removes_only_target_row_and_is_idempotent(self):
        self.store.import_samples(
            [
                Sample("sample_001", "note 1", "course 1", "judge 1"),
                Sample("sample_002", "note 2", "course 2", "judge 2"),
            ]
        )
        scores = {
            "completeness": 5,
            "correctness": 5,
            "readability": 5,
            "overall_quality": 5,
        }
        deductions = {"completeness": [], "correctness": {}, "readability": []}
        for evaluator_id in ("evaluator_a", "evaluator_b"):
            for sample_id in ("sample_001", "sample_002"):
                self.store.save_evaluation(
                    evaluator_id=evaluator_id,
                    sample_id=sample_id,
                    display_order=1,
                    scores=scores,
                    deductions=deductions,
                    detailed_comment="complete",
                    timer={},
                    llm_judge_view={},
                )

        # Deleting one (evaluator, sample) pair leaves every other row intact.
        self.assertTrue(self.store.delete_evaluation("evaluator_a", "sample_001"))
        self.assertIsNone(self.store.get_evaluation("evaluator_a", "sample_001"))
        self.assertIsNotNone(self.store.get_evaluation("evaluator_a", "sample_002"))
        self.assertIsNotNone(self.store.get_evaluation("evaluator_b", "sample_001"))

        # Idempotent: a second delete (nothing to remove) reports False, no raise.
        self.assertFalse(self.store.delete_evaluation("evaluator_a", "sample_001"))
        # Users and samples are untouched.
        self.assertIsNotNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertEqual(len(self.store.list_samples_for_evaluator("evaluator_a")), 2)

    def test_initialize_migrates_existing_evaluations_table_missing_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                create table samples (
                  sample_id text primary key,
                  discharge_note text not null,
                  brief_hospital_course text not null,
                  llm_judge text not null default '',
                  metadata_json text not null default '{}'
                );
                create table evaluations (
                  evaluator_id text not null,
                  sample_id text not null,
                  display_order integer not null,
                  completeness_score integer not null,
                  correctness_score integer not null,
                  readability_score integer not null,
                  deductions_json text not null,
                  primary key (evaluator_id, sample_id)
                );
                insert into samples (sample_id, discharge_note, brief_hospital_course)
                  values ('s1', 'note', 'course');
                insert into evaluations
                  (evaluator_id, sample_id, display_order, completeness_score,
                   correctness_score, readability_score, deductions_json)
                  values ('evaluator_a', 's1', 1, 5, 5, 5, '{}');
                """
            )
            conn.commit()
            conn.close()

            store = DoctorEvalStore(db_path)
            store.initialize()
            try:
                cols = {
                    r["name"]
                    for r in store.conn.execute("pragma table_info(evaluations)")
                }
                for required in (
                    "completed",
                    "overall_quality",
                    "detailed_comment",
                    "reset_count",
                    "llm_judge_visible_seconds",
                ):
                    self.assertIn(required, cols)
                # The pre-existing evaluation row survives and reads back cleanly.
                row = store.get_evaluation("evaluator_a", "s1")
                self.assertIsNotNone(row)
                self.assertEqual(row["scores"]["completeness"], 5)
                self.assertEqual(row["completed"], 0)
                self.assertEqual(row["reset_count"], 0)
            finally:
                store.close()

    def test_reset_evaluations_deletes_records_but_keeps_users_and_samples(self):
        self.store.import_samples(
            [Sample("sample_001", "note 1", "course 1", "judge 1")]
        )
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores={
                "completeness": 5,
                "correctness": 5,
                "readability": 5,
                "overall_quality": 5,
            },
            deductions={"completeness": [], "correctness": [], "readability": []},
            detailed_comment="complete",
            timer={},
            llm_judge_view={},
        )

        self.store.reset_evaluations()

        self.assertEqual(self.store.export_rows(), [])
        self.assertIsNotNone(self.store.authenticate("evaluator_a", "pass1234a"))
        self.assertEqual(len(self.store.list_samples_for_evaluator("evaluator_a")), 1)
        self.assertEqual(
            self.store.evaluator_progress(),
            [
                {"evaluator_id": "evaluator_a", "completed": 0, "total": 1},
                {"evaluator_id": "evaluator_b", "completed": 0, "total": 1},
                {"evaluator_id": "evaluator_c", "completed": 0, "total": 1},
            ],
        )

    def test_export_round_trip_preserves_every_field_through_csv_and_jsonl(self):
        import csv

        from doctor_eval_app.exporting import write_csv, write_jsonl

        self.store.import_samples(
            [Sample("s1", "퇴원요약1", "경과1", "judge1"), Sample("s2", "n2", "c2", "j2")]
        )
        comment = '환자 상태: =SUM(1)\n둘째 줄, "인용" 포함\t탭'
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="s1",
            display_order=1,
            scores={"completeness": 1, "correctness": 1, "readability": 3, "overall_quality": 2},
            deductions={
                "completeness": [
                    "missing_reason",
                    "missing_condition",
                    "missing_treatment",
                    "course_not_documented",
                ],
                "correctness": {"false_information": 3, "causal_error": 2},
                "readability": ["grammar", "duplicated"],
            },
            detailed_comment=comment,
            timer={
                "started_at": "2026-06-15T09:00:00+09:00",
                "ended_at": "2026-06-15T09:05:00+09:00",
                "active_seconds": 300,
                "paused_seconds": 12,
                "pause_count": 1,
                "reset_count": 2,
            },
            llm_judge_view={
                "opened": True,
                "first_opened_at": "2026-06-15T09:01:00+09:00",
                "open_count": 2,
                "visible_seconds": 40,
            },
        )
        # A draft evaluation must still export, flagged completed=0, with no overall.
        self.store.save_evaluation(
            evaluator_id="evaluator_b",
            sample_id="s1",
            display_order=1,
            scores={"completeness": 5, "correctness": 5, "readability": 5, "overall_quality": None},
            deductions={"completeness": [], "correctness": {}, "readability": []},
            detailed_comment="draft",
            timer={},
            llm_judge_view={},
            completed=False,
        )

        rows = self.store.export_rows()
        self.assertEqual(len(rows), 2)

        with tempfile.TemporaryDirectory() as out:
            csv_path = Path(out) / "r.csv"
            jsonl_path = Path(out) / "r.jsonl"
            write_csv(rows, csv_path)
            write_jsonl(rows, jsonl_path)
            with csv_path.open(encoding="utf-8", newline="") as handle:
                csv_rows = {row["evaluator_id"]: row for row in csv.DictReader(handle)}
            jsonl_rows = {
                json.loads(line)["evaluator_id"]: json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            }

        a_csv = csv_rows["evaluator_a"]
        a_json = jsonl_rows["evaluator_a"]
        b_json = jsonl_rows["evaluator_b"]

        # Scores survive exactly in both formats.
        self.assertEqual(a_csv["completeness_score"], "1")
        self.assertEqual(a_csv["correctness_score"], "1")
        self.assertEqual(a_csv["overall_quality"], "2")
        self.assertEqual(a_json["scores"], {
            "completeness": 1,
            "correctness": 1,
            "readability": 3,
            "overall_quality": 2,
        })
        # Completeness/readability selections and correctness counts survive.
        self.assertEqual(a_csv["completeness_missing_reason"], "1")
        self.assertEqual(a_csv["completeness_course_not_documented"], "1")
        self.assertEqual(a_csv["correctness_false_information_count"], "3")
        self.assertEqual(a_csv["correctness_causal_error_count"], "2")
        self.assertEqual(
            sorted(a_json["deductions"]["completeness"]),
            ["course_not_documented", "missing_condition", "missing_reason", "missing_treatment"],
        )
        self.assertEqual(a_json["deductions"]["correctness"], {"false_information": 3, "causal_error": 2})
        self.assertEqual(sorted(a_json["deductions"]["readability"]), ["duplicated", "grammar"])
        # JSONL keeps the comment verbatim; CSV preserves it too (newline/tab survive
        # quoting, and the formula char is mid-cell so injection guard does not fire).
        self.assertEqual(a_json["detailed_comment"], comment)
        self.assertEqual(a_csv["detailed_comment"], comment)
        # Timer + LLM telemetry survive.
        self.assertEqual(a_json["timer"]["active_seconds"], 300)
        self.assertEqual(a_json["timer"]["pause_count"], 1)
        self.assertEqual(a_json["llm_judge_view"]["open_count"], 2)
        self.assertEqual(a_csv["llm_judge_visible_seconds"], "40")
        # Draft is exported truthfully and never leaks an overall judgment.
        self.assertEqual(b_json["completed"], False)
        self.assertEqual(b_json["scores"]["overall_quality"], None)
        self.assertEqual(csv_rows["evaluator_b"]["completed"], "0")
        # No raw clinical text leaks into exports.
        self.assertNotIn("discharge_note", a_csv)
        self.assertNotIn("discharge_note", a_json)

    def test_resaving_evaluation_overwrites_in_place_without_duplicates(self):
        self.store.import_samples([Sample("s1", "n", "c", "j")])
        first = dict(
            evaluator_id="evaluator_a",
            sample_id="s1",
            display_order=1,
            scores={"completeness": 1, "correctness": 1, "readability": 1, "overall_quality": 1},
            deductions={"completeness": ["missing_reason"], "correctness": {}, "readability": []},
            detailed_comment="first",
            timer={"active_seconds": 10},
            llm_judge_view={},
        )
        self.store.save_evaluation(**first)
        revised = dict(first)
        revised.update(
            scores={"completeness": 4, "correctness": 5, "readability": 5, "overall_quality": 5},
            deductions={"completeness": [], "correctness": {}, "readability": []},
            detailed_comment="revised",
            timer={"active_seconds": 99},
        )
        self.store.save_evaluation(**revised)

        rows = self.store.export_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["completeness_score"], 4)
        self.assertEqual(rows[0]["overall_quality"], 5)
        self.assertEqual(rows[0]["detailed_comment"], "revised")
        self.assertEqual(rows[0]["active_seconds"], 99)
        self.assertEqual(rows[0]["completeness_missing_reason"], 0)


if __name__ == "__main__":
    unittest.main()
