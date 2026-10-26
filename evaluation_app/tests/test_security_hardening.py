"""Tests for the security hardening pass.

Each test here pins a specific security property so it cannot silently
regress. Where a guard is a deliberate trade-off (the actor gate is a no-op for
an unbound store) the test states the boundary explicitly rather than
pretending the guard is unconditional.
"""

import inspect
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

import _accounts
from doctor_eval_app.models import Sample, User
from doctor_eval_app.storage import (
    MIN_PASSWORD_LENGTH,
    DoctorEvalStore,
    pseudonymize_id,
)


VALID_DEDUCTIONS = {"completeness": [], "correctness": {}, "readability": []}


def _scores():
    return {
        "completeness": 4,
        "correctness": 4,
        "readability": 4,
        "overall_quality": 4,
    }


class SeedingTest(unittest.TestCase):
    """A removed account must not come back on the next launch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_deleted_evaluator_does_not_resurrect_on_reinitialize(self):
        self.store.delete_evaluator_account("evaluator_a")
        self.assertIsNone(self.store.authenticate("evaluator_a", "pass1234a"))

        # Simulate the next application launch against the same database.
        self.store.initialize()
        self.assertIsNone(self.store.authenticate("evaluator_a", "pass1234a"))

        reopened = DoctorEvalStore(self.db_path)
        try:
            reopened.initialize()
            self.assertIsNone(reopened.authenticate("evaluator_a", "pass1234a"))
        finally:
            reopened.close()

    def test_rotated_password_is_not_reverted_by_reinitialize(self):
        self.store.set_password("evaluator_b", "rotated-password")
        self.store.initialize()
        self.assertIsNone(self.store.authenticate("evaluator_b", "pass1234b"))
        self.assertIsNotNone(
            self.store.authenticate("evaluator_b", "rotated-password")
        )

    def test_seed_accounts_are_seeded_into_an_empty_database(self):
        seeded = {user_id for user_id, *_ in _accounts.TEST_ACCOUNTS}
        for user_id in seeded:
            self.assertIsNotNone(
                self.store.conn.execute(
                    "select user_id from users where user_id = ?", (user_id,)
                ).fetchone()
            )

    def test_shipped_app_seeds_no_accounts(self):
        # _accounts overrides the class default for the test suite, so pin the
        # value it captured before overriding: that is what the release seeds.
        self.assertEqual(_accounts.SHIPPED_SEED_ACCOUNTS, ())
        fresh = DoctorEvalStore(Path(self.tmp.name) / "fresh.db")
        fresh.seed_accounts = _accounts.SHIPPED_SEED_ACCOUNTS
        try:
            fresh.initialize()
            count = fresh.conn.execute("select count(*) as n from users").fetchone()
            self.assertEqual(count["n"], 0)
            self.assertFalse(fresh.has_admin())
        finally:
            fresh.close()

    def test_concurrent_first_run_setup_creates_only_one_admin(self):
        """Two windows on the same fresh database both pass the has_admin()
        check before either inserts; the insert itself must refuse the second."""
        path = Path(self.tmp.name) / "race.db"
        first, second = DoctorEvalStore(path), DoctorEvalStore(path)
        for store in (first, second):
            store.seed_accounts = ()
            store.initialize()
            self.addCleanup(store.close)
        self.assertFalse(first.has_admin())
        self.assertFalse(second.has_admin())

        first.create_initial_admin("alice_adm", "pw-123456", "pw-123456")
        # Simulate second having checked before first committed.
        second.has_admin = lambda: False
        with self.assertRaises(PermissionError):
            second.create_initial_admin("mallory", "pw-123456", "pw-123456")
        with self.assertRaises(ValueError):
            second.create_initial_admin("alice_adm", "pw-123456", "pw-123456")

        admins = first.conn.execute(
            "select user_id from users where role = 'admin'"
        ).fetchall()
        self.assertEqual([row["user_id"] for row in admins], ["alice_adm"])

    def test_first_run_setup_is_closed_inside_a_session(self):
        fresh = DoctorEvalStore(Path(self.tmp.name) / "fresh2.db")
        fresh.seed_accounts = ()
        fresh.initialize()
        self.addCleanup(fresh.close)
        fresh.set_actor(User(user_id="someone", role="evaluator"))

        with self.assertRaises(PermissionError):
            fresh.create_initial_admin("sneaky", "pw-123456", "pw-123456")
        self.assertFalse(fresh.has_admin())

    def test_first_run_admin_cannot_be_created_twice(self):
        # The fixture admin exists, so the unauthenticated first-run path is shut.
        with self.assertRaises(PermissionError):
            self.store.create_initial_admin("intruder", "pw-123456", "pw-123456")
        self.assertIsNone(self.store.authenticate("intruder", "pw-123456"))


class ForcedPasswordChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_seeded_admin_must_change_password(self):
        user = self.store.authenticate("admin", "admin")
        self.assertIsNotNone(user)
        self.assertTrue(user.must_change_password)

    def test_set_password_clears_the_requirement(self):
        self.assertTrue(self.store.set_password("admin", "a-better-password"))
        user = self.store.authenticate("admin", "a-better-password")
        self.assertIsNotNone(user)
        self.assertFalse(user.must_change_password)

    def test_refused_rotation_keeps_the_requirement(self):
        with self.assertRaises(ValueError):
            self.store.set_password("admin", "short")
        # Still flagged, so the forced-change dialog will come back.
        self.assertTrue(self.store.authenticate("admin", "admin").must_change_password)

    def test_password_must_meet_minimum_length(self):
        with self.assertRaises(ValueError):
            self.store.set_password("admin", "a" * (MIN_PASSWORD_LENGTH - 1))
        self.assertTrue(
            self.store.set_password("admin", "a" * MIN_PASSWORD_LENGTH)
        )

    def test_legacy_database_without_the_column_flags_admin_on_migration(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            create table users (
              user_id text primary key,
              role text not null,
              password_salt text not null,
              password_hash text not null
            );
            insert into users values ('admin', 'admin', 'x', 'y');
            """
        )
        conn.commit()
        conn.close()

        store = DoctorEvalStore(legacy_path)
        try:
            store.initialize()
            row = store.conn.execute(
                "select must_change_password from users where user_id = 'admin'"
            ).fetchone()
            self.assertEqual(row["must_change_password"], 1)
        finally:
            store.close()


class ActorGateTest(unittest.TestCase):
    """Admin-only storage operations refuse a bound evaluator identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()
        self.store.import_samples([Sample("sample_001", "note", "course", "judge")])
        self.evaluator = User(user_id="evaluator_a", role="evaluator")
        self.admin = User(user_id="admin", role="admin")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_unbound_store_is_trusted_local_access(self):
        # The documented boundary: no actor means a direct local caller (tests,
        # migrations, scripts), not an unauthenticated user of the running app.
        self.assertIsNone(self.store._actor)
        self.store.reset_evaluations()  # must not raise

    def test_evaluator_actor_cannot_run_admin_operations(self):
        self.store.set_actor(self.evaluator)
        for label, call in [
            ("reset_evaluations", lambda: self.store.reset_evaluations()),
            (
                "sample_review_comparison_details",
                lambda: self.store.sample_review_comparison_details(),
            ),
            (
                "delete_evaluator_account",
                lambda: self.store.delete_evaluator_account("evaluator_b"),
            ),
            (
                "create_evaluator_account",
                lambda: self.store.create_evaluator_account(
                    "new_eval", "pw-123456", "pw-123456"
                ),
            ),
        ]:
            with self.subTest(operation=label):
                with self.assertRaises(PermissionError):
                    call()

    def test_admin_actor_may_run_admin_operations(self):
        self.store.set_actor(self.admin)
        self.store.reset_evaluations()
        self.assertIsInstance(self.store.sample_review_comparison_details(), list)

    def test_evaluator_actor_is_confined_to_its_own_rows(self):
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="sample_001",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="mine",
            timer={},
            llm_judge_view={},
        )
        self.store.set_actor(self.evaluator)

        own = self.store.export_rows("evaluator_a")
        self.assertEqual(len(own), 1)

        with self.assertRaises(PermissionError):
            self.store.export_rows("evaluator_b")
        with self.assertRaises(PermissionError):
            self.store.export_rows()  # unscoped == every evaluator
        with self.assertRaises(PermissionError):
            self.store.completed_evaluation_details("evaluator_b")

    def test_full_gate_matrix(self):
        """Every sensitive store method, under both roles. Pins the whole
        surface so a newly added method is a visible gap, not a silent one."""
        admin_only = [
            ("list_users", lambda: self.store.list_users()),
            ("evaluator_progress", lambda: self.store.evaluator_progress()),
            ("evaluator_score_summary", lambda: self.store.evaluator_score_summary()),
            ("progress_summary", lambda: self.store.progress_summary()),
            ("import_samples", lambda: self.store.import_samples([])),
            ("import_evaluations", lambda: self.store.import_evaluations([])),
        ]
        cross_evaluator = [
            ("get_evaluation", lambda: self.store.get_evaluation("evaluator_b", "sample_001")),
            ("delete_evaluation", lambda: self.store.delete_evaluation("evaluator_b", "sample_001")),
            ("set_password", lambda: self.store.set_password("admin", "another-password")),
        ]

        self.store.set_actor(self.evaluator)
        for label, call in admin_only + cross_evaluator:
            with self.subTest(role="evaluator", operation=label):
                with self.assertRaises(PermissionError):
                    call()

        # An evaluator still reaches their own row and their own password.
        self.store.get_evaluation("evaluator_a", "sample_001")
        self.assertTrue(self.store.set_password("evaluator_a", "own-new-password"))

        self.store.set_actor(self.admin)
        for label, call in admin_only + cross_evaluator:
            with self.subTest(role="admin", operation=label):
                call()  # must not raise

    def test_every_public_method_is_gated_or_explicitly_exempt(self):
        """Derived from the class itself, not a hand-written list.

        The first version of this suite enumerated the methods it knew were
        guarded, so it stayed green while eleven others sat wide open. This
        walks the whole public surface instead: a newly added method fails here
        until it is either guarded or consciously listed as exempt.
        """
        exempt = {
            "close": "closes the connection, touches no data",
            "set_actor": "establishes the identity the gates read",
            "initialize": "schema bootstrap, runs before any login",
            "backup_database": "internal helper invoked by gated operations",
            "authenticate": "must run before an identity exists",
            "login_block_reason": "login screen feedback, pre-authentication",
            "password_change_required": "read during the forced-change flow",
            "has_admin": "decides between first-run setup and login, pre-authentication",
            "create_initial_admin": "first-run setup; refuses once an admin exists",
        }

        gated, ungated = [], []
        for name in dir(DoctorEvalStore):
            if name.startswith("_"):
                continue
            attr = getattr(DoctorEvalStore, name)
            if not callable(attr):
                continue
            try:
                source = inspect.getsource(attr)
            except (OSError, TypeError):
                continue
            if "_require_admin(" in source or "_require_own_scope(" in source:
                gated.append(name)
            else:
                ungated.append(name)

        self.assertEqual(
            sorted(set(ungated) - set(exempt)),
            [],
            "ungated public store method(s); add a guard or an exemption reason",
        )
        # The exemption list must not rot into a place methods hide.
        self.assertEqual(sorted(set(exempt) - set(ungated)), [])
        self.assertGreater(len(gated), 15)

    def test_logout_clears_the_actor(self):
        self.store.set_actor(self.evaluator)
        self.store.set_actor(None)
        self.store.reset_evaluations()  # trusted again, must not raise


class OnDiskExposureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "storage" / "doctor_eval.db"
        self.store = DoctorEvalStore(self.db_path)
        self.store.initialize()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_database_and_directory_are_owner_only(self):
        self.assertEqual(stat.S_IMODE(self.db_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.db_path.parent.stat().st_mode), 0o700)

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits only")
    def test_backups_are_owner_only(self):
        dest = self.store.backup_database("test_reason")
        self.assertIsNotNone(dest)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(dest.parent.stat().st_mode), 0o700)

    def test_backup_filename_carries_no_patient_identifier(self):
        self.store.import_samples([Sample("demo_01_A", "note", "course", "judge")])
        self.store.save_evaluation(
            evaluator_id="evaluator_a",
            sample_id="demo_01_A",
            display_order=1,
            scores=_scores(),
            deductions=VALID_DEDUCTIONS,
            detailed_comment="x",
            timer={},
            llm_judge_view={},
        )
        self.store.delete_evaluation("evaluator_a", "demo_01_A")
        backups = list((self.db_path.parent / "backups").glob("*.db"))
        self.assertTrue(backups)
        for path in backups:
            self.assertNotIn("demo_01_A", path.name)

    def test_pseudonymize_is_stable_and_does_not_encode_the_source_id(self):
        # "the digest does not contain the decimal id" is nearly free for a hex
        # string, so assert the properties that actually carry weight: stability
        # (an admin can correlate), collision-freedom over a realistic id space, and
        # a digest that is not a reversible encoding of the input.
        real_ids = [f"demo_{i:03d}_A" for i in range(200)]
        digests = [pseudonymize_id(i) for i in real_ids]
        self.assertEqual(len(set(digests)), len(real_ids))
        self.assertEqual(digests, [pseudonymize_id(i) for i in real_ids])
        for source, digest in zip(real_ids, digests):
            self.assertNotIn(source, digest)
            self.assertNotIn(digest, source)
            # Not a truncation, reversal or trivial transform of the input.
            self.assertNotEqual(digest, source[: len(digest)])
            self.assertNotEqual(digest, source[::-1][: len(digest)])
            self.assertTrue(all(c in "0123456789abcdef" for c in digest))


if __name__ == "__main__":
    unittest.main()
