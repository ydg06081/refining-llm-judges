import hashlib
import hmac
import json
import os
import random
import re
import shutil
import sqlite3
from datetime import datetime
from itertools import groupby
from pathlib import Path

from doctor_eval_app.models import Sample, User
from doctor_eval_app.scoring import normalize_correctness_counts


MIN_PASSWORD_LENGTH = 8
# Account IDs end up in export rows, file names and log lines, so keep them to a
# conservative, filesystem-safe alphabet. A trailing '.' or '-' is refused
# because file names strip it, which would make "kim." and "kim" share files.
USER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,30}[A-Za-z0-9_]")
USER_ID_RULE = (
    "ID must be 3-32 characters: letters, digits, '_', '.' or '-', "
    "starting with a letter or digit and not ending in '.' or '-'"
)

# Batch 1's seed is the bare evaluator_id. Changing it would reshuffle (and
# renumber) every sample an evaluator has already completed, so do not "tidy"
# this into a uniform f"{evaluator_id}:batch1".
FIRST_BATCH = 1


def batch_shuffle_seed(evaluator_id, batch):
    if batch == FIRST_BATCH:
        return evaluator_id
    return f"{evaluator_id}:batch{batch}"


def _validate_new_account(user_id, password, password_check):
    """Shared checks for every account created through the app.

    Returns the normalised user_id. Raises ValueError with a message that can be
    shown to the user as-is.
    """
    user_id = (user_id or "").strip()
    password = password or ""
    if not user_id:
        raise ValueError("ID is required")
    if not USER_ID_PATTERN.fullmatch(user_id):
        raise ValueError(USER_ID_RULE)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if password != (password_check or ""):
        raise ValueError("Passwords do not match")
    return user_id


COMPLETENESS_DEDUCTION_KEYS = (
    "missing_reason",
    "missing_condition",
    "missing_treatment",
    "course_not_documented",
)



class EvaluationSaveError(RuntimeError):
    pass


class DoctorEvalStore:
    # Accounts seeded into a brand-new (empty) users table, as
    # (user_id, password, role, name, must_change_password) tuples.
    #
    # Empty in the shipped app: no account exists until the first launch, where
    # the person installing the app creates the admin account interactively
    # (see ``create_initial_admin``), and the admin then creates evaluator
    # accounts from the dashboard (``create_evaluator_account``). The test suite
    # sets this to fixed fixture accounts (tests/_accounts.py).
    seed_accounts = ()

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        _restrict_dir(self.db_path.parent)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma synchronous=FULL")
        self.conn.execute("pragma foreign_keys=ON")
        # The DB holds raw clinical text and password hashes; keep it owner-only.
        # sqlite3.connect creates the file, so this has to run after connecting.
        _restrict_file(self.db_path)
        # No actor bound means trusted local access (tests, migrations, scripts).
        # The UI binds the authenticated user at login, which is what makes the
        # admin gates below effective for anything reachable from the app.
        self._actor = None

    def close(self):
        self.conn.close()

    def set_actor(self, user):
        """Bind the authenticated user whose authority later calls run under.

        Pass ``None`` to clear on logout. Admin-only operations check this, so a
        UI wiring mistake cannot reach them from an evaluator session.

        The actor is a login-time snapshot: role and status are not re-read from
        the database, so an account demoted or rejected mid-session keeps its
        authority until logout. Acceptable for a single-user desktop app where
        the admin performing the change is the one holding the session.
        """
        self._actor = user

    def _require_admin(self, action):
        actor = self._actor
        if actor is None:
            return
        if getattr(actor, "role", None) != "admin":
            raise PermissionError(f"{action} requires an admin account")

    def _require_own_scope(self, evaluator_id, action):
        """Admins see everything; an evaluator is confined to their own rows.

        ``evaluator_id=None`` means unscoped (all evaluators), so it is admin-only.
        """
        actor = self._actor
        if actor is None or getattr(actor, "role", None) == "admin":
            return
        if evaluator_id is None or evaluator_id != getattr(actor, "user_id", None):
            raise PermissionError(f"{action} requires an admin account")

    def backup_database(self, reason, keep=5):
        """Snapshot the SQLite file before a destructive operation.

        Backups land in ``storage/backups/`` with an ``auto-`` prefix so the
        retention sweep never touches manually created backup files. Only the
        most recent ``keep`` automatic snapshots are retained. Any failure to
        back up (missing DB on first run, read-only disk) is swallowed: a backup
        must never block or crash the operation it is protecting, but it also
        must not silently mask a real failure of that operation.
        """
        if not self.db_path.exists():
            return None
        backups_dir = self.db_path.parent / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        _restrict_dir(backups_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_reason = _safe_backup_reason(reason)
        dest = backups_dir / f"{self.db_path.stem}.auto-{safe_reason}-{stamp}.db"
        try:
            # Flush WAL/pending writes so the copy is a consistent snapshot.
            self.conn.commit()
            shutil.copy2(self.db_path, dest)
        except OSError:
            return None
        # Each snapshot is a full copy of the clinical text: same lockdown as the
        # live DB, otherwise the backups become the soft target.
        _restrict_file(dest)
        self._prune_auto_backups(backups_dir, keep)
        return dest

    def _prune_auto_backups(self, backups_dir, keep):
        def sort_key(path):
            # Order by the timestamp embedded in the filename, NOT by st_mtime.
            # shutil.copy2 copies the SOURCE db's mtime onto the backup, so after
            # a quiet period the newest snapshot can carry an older mtime than an
            # earlier one -- sorting by mtime then prunes the newest first. The
            # filename stamp (%Y%m%d-%H%M%S-%f) is the real time the backup was
            # taken, is fixed-width and zero-padded, so it sorts correctly
            # lexically regardless of the reason text (which contains no '-').
            stamp = _backup_stamp(path.name)
            if stamp is not None:
                return (0, stamp, path.name)
            # No parseable stamp (hand-named file): fall back to mtime, and sort
            # these before stamped ones so a stray file is pruned before a real
            # auto snapshot.
            try:
                return (1, path.stat().st_mtime, path.name)
            except OSError:
                return (1, 0.0, path.name)

        snapshots = sorted(
            backups_dir.glob(f"{self.db_path.stem}.auto-*.db"),
            key=sort_key,
        )
        for stale in snapshots[:-keep] if keep > 0 else snapshots:
            try:
                stale.unlink()
            except OSError:
                pass

    def initialize(self):
        self.conn.executescript(
            """
            create table if not exists users (
              user_id text primary key,
              role text not null,
              password_salt text not null,
              password_hash text not null
            );

            create table if not exists samples (
              sample_id text primary key,
              discharge_note text not null,
              brief_hospital_course text not null,
              llm_judge text not null default '',
              metadata_json text not null default '{}',
              batch integer not null default 1
            );

            create table if not exists evaluations (
              evaluator_id text not null,
              sample_id text not null,
              display_order integer not null,
              completed integer not null default 0,
              completeness_score integer not null,
              correctness_score integer not null,
              readability_score integer not null,
              overall_quality integer,
              deductions_json text not null,
              detailed_comment text not null default '',
              started_at text,
              ended_at text,
              active_seconds integer not null default 0,
              paused_seconds integer not null default 0,
              pause_count integer not null default 0,
              reset_count integer not null default 0,
              llm_judge_opened integer not null default 0,
              llm_judge_first_opened_at text,
              llm_judge_open_count integer not null default 0,
              llm_judge_visible_seconds integer not null default 0,
              primary key (evaluator_id, sample_id),
              foreign key (sample_id) references samples(sample_id)
            );
            """
        )
        self._ensure_user_columns()
        self._ensure_evaluation_columns()
        self._ensure_sample_columns()
        self.conn.commit()
        self._seed_accounts()

    def _seed_accounts(self):
        """Create ``seed_accounts``, but only into a brand-new database.

        Seeding only when the table is empty makes a deletion stick: otherwise
        a deleted account would be re-created on the next launch.
        """
        if not self.seed_accounts:
            return
        existing = self.conn.execute("select count(*) as n from users").fetchone()
        if existing["n"]:
            return
        for user_id, password, role, name, must_change in self.seed_accounts:
            self.create_user_if_missing(user_id, password, role, name=name)
            if must_change:
                with self.conn:
                    self.conn.execute(
                        "update users set must_change_password = 1 where user_id = ?",
                        (user_id,),
                    )

    def has_admin(self):
        """Whether an approved admin account exists.

        Read before anyone has logged in: the UI uses it to decide between the
        first-run admin setup screen and the normal login screen.
        """
        row = self.conn.execute(
            "select count(*) as n from users where role = 'admin' and status = 'approved'"
        ).fetchone()
        return row["n"] > 0

    def _ensure_evaluation_columns(self):
        """Forward-migrate an evaluations table created by an older version.

        ``create table if not exists`` never adds columns to a pre-existing
        table, so an evaluator upgrading in place over an older DB would be
        missing the telemetry columns this code reads/writes. Mirror
        ``_ensure_user_columns``: additively add any missing column (all with
        safe defaults / nullable) so existing evaluations are preserved and
        keep working. Base key/score columns are not migrated here because any
        DB old enough to have an evaluations table already has them.
        """
        columns = {
            row["name"]
            for row in self.conn.execute("pragma table_info(evaluations)").fetchall()
        }
        if not columns:
            return
        migrations = {
            "completed": "alter table evaluations add column completed integer not null default 0",
            "overall_quality": "alter table evaluations add column overall_quality integer",
            "detailed_comment": "alter table evaluations add column detailed_comment text not null default ''",
            "started_at": "alter table evaluations add column started_at text",
            "ended_at": "alter table evaluations add column ended_at text",
            "active_seconds": "alter table evaluations add column active_seconds integer not null default 0",
            "paused_seconds": "alter table evaluations add column paused_seconds integer not null default 0",
            "pause_count": "alter table evaluations add column pause_count integer not null default 0",
            "reset_count": "alter table evaluations add column reset_count integer not null default 0",
            "llm_judge_opened": "alter table evaluations add column llm_judge_opened integer not null default 0",
            "llm_judge_first_opened_at": "alter table evaluations add column llm_judge_first_opened_at text",
            "llm_judge_open_count": "alter table evaluations add column llm_judge_open_count integer not null default 0",
            "llm_judge_visible_seconds": "alter table evaluations add column llm_judge_visible_seconds integer not null default 0",
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.conn.execute(statement)

    def _ensure_sample_columns(self):
        """Forward-migrate a samples table created before batching existed.

        Every sample in a pre-batch database belongs to its first round, so
        ``default 1`` is the correct backfill: batch 1 keeps the exact
        presentation order those evaluators already worked through, and any
        later batch is appended after it (see ``list_samples_for_evaluator``).
        """
        columns = {
            row["name"]
            for row in self.conn.execute("pragma table_info(samples)").fetchall()
        }
        if not columns:
            return
        if "batch" not in columns:
            self.conn.execute(
                "alter table samples add column batch integer not null default 1"
            )

    def _ensure_user_columns(self):
        columns = {
            row["name"]
            for row in self.conn.execute("pragma table_info(users)").fetchall()
        }
        migrations = {
            "name": "alter table users add column name text not null default ''",
            "status": "alter table users add column status text not null default 'approved'",
            "requested_at": "alter table users add column requested_at text",
            "approved_at": "alter table users add column approved_at text",
            "rejected_at": "alter table users add column rejected_at text",
            "must_change_password": (
                "alter table users add column must_change_password "
                "integer not null default 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                self.conn.execute(statement)
        if columns and "must_change_password" not in columns:
            # Upgrading a DB that predates the flag: require the admin to set a
            # new password at next login.
            self.conn.execute(
                "update users set must_change_password = 1 where role = 'admin'"
            )
        self.conn.execute(
            "update users set name = user_id where name is null or name = ''"
        )
        self.conn.execute(
            "update users set status = 'approved' where status is null or status = ''"
        )

    def create_user_if_missing(
        self,
        user_id,
        password,
        role,
        name=None,
        status="approved",
    ):
        # Creates accounts of any role, so it is admin-only once a session is
        # bound. Seeding (``_seed_accounts``) runs unbound.
        self._require_admin("Creating an account")
        existing = self.conn.execute(
            "select user_id from users where user_id = ?",
            (user_id,),
        ).fetchone()
        if existing:
            return

        salt, password_hash = _hash_password(password)
        self.conn.execute(
            """
            insert into users (
              user_id,
              role,
              password_salt,
              password_hash,
              name,
              status,
              requested_at,
              approved_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                role,
                salt,
                password_hash,
                name or user_id,
                status,
                _now_iso() if status == "pending" else None,
                _now_iso() if status == "approved" else None,
            ),
        )
        self.conn.commit()

    def set_password(self, user_id, password):
        """Rotate a password and clear any outstanding change requirement."""
        # An evaluator may rotate their own password and nobody else's.
        self._require_own_scope(user_id, "Changing another account's password")
        password = password or ""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        salt, password_hash = _hash_password(password)
        with self.conn:
            cursor = self.conn.execute(
                "update users set password_salt = ?, password_hash = ?, "
                "must_change_password = 0 "
                "where user_id = ?",
                (salt, password_hash, user_id),
            )
        return cursor.rowcount > 0

    def password_change_required(self, user_id):
        """Whether this account still owes a password rotation.

        Read straight from the row so callers can re-check the stored state
        instead of trusting a dialog's return code. Fails closed: an account
        that has vanished counts as still owing one.
        """
        row = self.conn.execute(
            "select must_change_password from users where user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return True
        return bool(row["must_change_password"])

    def authenticate(self, user_id, password):
        row = self.conn.execute(
            """
            select user_id, role, password_salt, password_hash, name, status,
                   must_change_password
            from users
            where user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "approved":
            return None

        if not _verify_password(password, row["password_salt"], row["password_hash"]):
            return None
        return User(
            user_id=row["user_id"],
            role=row["role"],
            name=row["name"],
            status=row["status"],
            must_change_password=bool(row["must_change_password"]),
        )

    def login_block_reason(self, user_id):
        row = self.conn.execute(
            "select status from users where user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return "missing"
        if row["status"] in {"pending", "rejected"}:
            return row["status"]
        return "invalid_password"

    def list_users(self, role=None):
        self._require_admin("Listing accounts")
        if role is None:
            rows = self.conn.execute(
                """
                select user_id, role, name, status
                from users
                where status = 'approved'
                order by role, user_id
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                select user_id, role, name, status
                from users
                where role = ? and status = 'approved'
                order by user_id
                """,
                (role,),
            ).fetchall()
        return [
            User(
                user_id=row["user_id"],
                role=row["role"],
                name=row["name"],
                status=row["status"],
            )
            for row in rows
        ]

    def create_initial_admin(self, user_id, password, password_check, name=None):
        """First-run setup: create the one admin account the app starts with.

        The shipped app has no accounts at all, so whoever installs it chooses
        the admin ID and password on the first launch, so no two installations
        share a credential. Allowed only while no admin exists; after
        that, this path is closed and accounts are managed from the dashboard.
        Returns the created ``User``.
        """
        # Unauthenticated by design, so it must not be reachable from inside a
        # logged-in session either.
        if self._actor is not None or self.has_admin():
            raise PermissionError("An admin account already exists")
        user_id = _validate_new_account(user_id, password, password_check)
        self._insert_account(user_id, password, "admin", name, only_if_no_admin=True)
        return User(user_id=user_id, role="admin", name=(name or "").strip() or user_id)

    def create_evaluator_account(self, user_id, password, password_check, name=None):
        """Admin creates an evaluator account that can log in immediately.

        This replaces self-service signup: evaluators no longer register from
        the login screen, the admin issues their ID and initial password.
        Returns the created ``User``.
        """
        self._require_admin("Creating an evaluator account")
        user_id = _validate_new_account(user_id, password, password_check)
        self._insert_account(user_id, password, "evaluator", name)
        return User(
            user_id=user_id, role="evaluator", name=(name or "").strip() or user_id
        )

    def _insert_account(self, user_id, password, role, name, only_if_no_admin=False):
        """Insert an approved account.

        IDs are unique case-insensitively: "Kim" and "kim" would share draft and
        export files on Windows/macOS. ``only_if_no_admin`` folds the first-run
        "no admin yet" check into the insert itself, so two windows submitting
        the setup screen at once cannot both create an admin.
        """
        existing = self.conn.execute(
            "select user_id from users where lower(user_id) = lower(?)",
            (user_id,),
        ).fetchone()
        if existing:
            raise ValueError("ID is already registered")
        salt, password_hash = _hash_password(password)
        guard = (
            " where not exists (select 1 from users"
            " where role = 'admin' and status = 'approved')"
            if only_if_no_admin
            else ""
        )
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    insert into users (
                      user_id,
                      role,
                      password_salt,
                      password_hash,
                      name,
                      status,
                      approved_at
                    )
                    select ?, ?, ?, ?, ?, 'approved', ?
                    """
                    + guard,
                    (
                        user_id,
                        role,
                        salt,
                        password_hash,
                        (name or "").strip() or user_id,
                        _now_iso(),
                    ),
                )
        except sqlite3.IntegrityError:
            # Lost a race with another window creating the same ID.
            raise ValueError("ID is already registered") from None
        if cursor.rowcount != 1:
            raise PermissionError("An admin account already exists")

    def delete_evaluator_account(self, user_id):
        self._require_admin("Deleting an account")
        row = self.conn.execute(
            """
            select user_id, role
            from users
            where user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if not row or row["role"] != "evaluator":
            raise ValueError("Evaluator account not found")

        self.backup_database(f"delete_account_{user_id}")
        with self.conn:
            self.conn.execute(
                "delete from evaluations where evaluator_id = ?",
                (user_id,),
            )
            self.conn.execute(
                "delete from users where user_id = ? and role = 'evaluator'",
                (user_id,),
            )

    def import_samples(self, samples, batch=1):
        """Insert a group of samples, tagged with the round (batch) they belong to.

        ``batch`` is what keeps a later top-up from disturbing work already done:
        presentation order is shuffled *within* a batch and batches are
        concatenated in ascending order, so batch 1 keeps the exact sequence its
        evaluators already saw and batch 2 lands after it. See
        ``list_samples_for_evaluator``.
        """
        self._require_admin("Importing samples")
        if batch < 1:
            raise ValueError(f"batch must be >= 1, got {batch}")
        seen = set()
        for sample in samples:
            if sample.sample_id in seen:
                raise ValueError(f"Duplicate sample_id: {sample.sample_id}")
            seen.add(sample.sample_id)

        # A sample_id that already exists would either fail the primary key or,
        # worse, be silently taken as "already imported" and shift a batch the
        # evaluators have already worked through. Refuse loudly instead.
        collisions = sorted(
            row["sample_id"]
            for row in self.conn.execute("select sample_id from samples").fetchall()
            if row["sample_id"] in seen
        )
        if collisions:
            raise ValueError(
                "These sample_ids already exist in the database: "
                f"{collisions[:5]}{'...' if len(collisions) > 5 else ''}"
            )

        with self.conn:
            for sample in samples:
                self.conn.execute(
                    """
                    insert into samples (
                      sample_id,
                      discharge_note,
                      brief_hospital_course,
                      llm_judge,
                      metadata_json,
                      batch
                    )
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sample.sample_id,
                        sample.discharge_note,
                        sample.brief_hospital_course,
                        sample.llm_judge,
                        json.dumps(sample.metadata, ensure_ascii=False),
                        batch,
                    ),
                )

    def list_samples_for_evaluator(self, evaluator_id):
        """Return this evaluator's samples in their personal presentation order.

        Order is derived, not stored, so it must stay stable for every sample an
        evaluator has already been shown. It is built per batch and the batches
        are concatenated in ascending order:

        * batch 1 is shuffled with the bare ``evaluator_id`` seed, over its rows
          sorted by ``sample_id`` -- byte-for-byte what this method returned
          before batches existed, so the first batch keeps the exact sequence
          and numbering its evaluators already worked through.
        * every later batch gets its own seed and is appended after the batches
          before it, so a top-up is numbered after everything already shown and
          nothing already completed moves.

        Shuffling the whole table at once would renumber and re-interleave every
        completed sample the moment a single new row was imported.
        """
        self._require_own_scope(
            evaluator_id, "Listing another evaluator's samples"
        )
        rows = self.conn.execute(
            """
            select
              samples.sample_id,
              samples.discharge_note,
              samples.brief_hospital_course,
              samples.llm_judge,
              samples.batch,
              coalesce(evaluations.completed, 0) as completed
            from samples
            left join evaluations
              on evaluations.sample_id = samples.sample_id
             and evaluations.evaluator_id = ?
            order by samples.batch, samples.sample_id
            """,
            (evaluator_id,),
        ).fetchall()

        ordered = []
        for batch, group in groupby(rows, key=lambda row: row["batch"]):
            group = list(group)
            random.Random(batch_shuffle_seed(evaluator_id, batch)).shuffle(group)
            ordered.extend(group)

        return [
            {
                "display_order": index + 1,
                "sample_id": row["sample_id"],
                "discharge_note": row["discharge_note"],
                "brief_hospital_course": row["brief_hospital_course"],
                "llm_judge": row["llm_judge"],
                "batch": row["batch"],
                "completed": row["completed"],
            }
            for index, row in enumerate(ordered)
        ]

    def batch_has_evaluations(self, batch):
        """True when any evaluator has saved an evaluation in this batch.

        Guards the one operation that can silently rewrite history: adding a
        sample to a round somebody has already started. Order inside a batch is
        derived from that batch's members, so a late insert moves samples that
        were already presented.
        """
        self._require_admin("Reading batch evaluation state")
        row = self.conn.execute(
            """
            select count(*) as n
            from evaluations
            join samples on samples.sample_id = evaluations.sample_id
            where samples.batch = ?
            """,
            (batch,),
        ).fetchone()
        return row["n"] > 0

    def sample_batches(self):
        """Map batch number -> the sample_ids imported in it."""
        self._require_admin("Listing sample batches")
        batches = {}
        for row in self.conn.execute(
            "select sample_id, batch from samples order by batch, sample_id"
        ).fetchall():
            batches.setdefault(row["batch"], []).append(row["sample_id"])
        return batches

    def save_evaluation(
        self,
        evaluator_id,
        sample_id,
        display_order,
        scores,
        deductions,
        detailed_comment,
        timer,
        llm_judge_view,
        completed=True,
    ):
        self._require_own_scope(
            evaluator_id, "Writing another evaluator's evaluation"
        )
        stored_deductions = dict(deductions)
        stored_deductions["correctness"] = normalize_correctness_counts(
            stored_deductions.get("correctness", {})
        )

        with self.conn:
            self.conn.execute(
                """
                insert into evaluations (
                  evaluator_id,
                  sample_id,
                  display_order,
                  completed,
                  completeness_score,
                  correctness_score,
                  readability_score,
                  overall_quality,
                  deductions_json,
                  detailed_comment,
                  started_at,
                  ended_at,
                  active_seconds,
                  paused_seconds,
                  pause_count,
                  reset_count,
                  llm_judge_opened,
                  llm_judge_first_opened_at,
                  llm_judge_open_count,
                  llm_judge_visible_seconds
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(evaluator_id, sample_id) do update set
                  display_order = excluded.display_order,
                  completed = excluded.completed,
                  completeness_score = excluded.completeness_score,
                  correctness_score = excluded.correctness_score,
                  readability_score = excluded.readability_score,
                  overall_quality = excluded.overall_quality,
                  deductions_json = excluded.deductions_json,
                  detailed_comment = excluded.detailed_comment,
                  started_at = excluded.started_at,
                  ended_at = excluded.ended_at,
                  active_seconds = excluded.active_seconds,
                  paused_seconds = excluded.paused_seconds,
                  pause_count = excluded.pause_count,
                  reset_count = excluded.reset_count,
                  llm_judge_opened = excluded.llm_judge_opened,
                  llm_judge_first_opened_at = excluded.llm_judge_first_opened_at,
                  llm_judge_open_count = excluded.llm_judge_open_count,
                  llm_judge_visible_seconds = excluded.llm_judge_visible_seconds
                """,
                (
                    evaluator_id,
                    sample_id,
                    display_order,
                    1 if completed else 0,
                    scores["completeness"],
                    scores["correctness"],
                    scores["readability"],
                    scores.get("overall_quality"),
                    json.dumps(stored_deductions, ensure_ascii=False),
                    detailed_comment,
                    timer.get("started_at"),
                    timer.get("ended_at"),
                    timer.get("active_seconds", 0),
                    timer.get("paused_seconds", 0),
                    timer.get("pause_count", 0),
                    timer.get("reset_count", 0),
                    1 if llm_judge_view.get("opened") else 0,
                    llm_judge_view.get("first_opened_at"),
                    llm_judge_view.get("open_count", 0),
                    llm_judge_view.get("visible_seconds", 0),
                ),
            )

    def get_evaluation(self, evaluator_id, sample_id):
        self._require_own_scope(evaluator_id, "Reading another evaluator's evaluation")
        row = self.conn.execute(
            """
            select
              evaluator_id,
              sample_id,
              display_order,
              completed,
              completeness_score,
              correctness_score,
              readability_score,
              overall_quality,
              deductions_json,
              detailed_comment,
              started_at,
              ended_at,
              active_seconds,
              paused_seconds,
              pause_count,
              reset_count,
              llm_judge_opened,
              llm_judge_first_opened_at,
              llm_judge_open_count,
              llm_judge_visible_seconds
            from evaluations
            where evaluator_id = ?
              and sample_id = ?
            """,
            (evaluator_id, sample_id),
        ).fetchone()
        if row is None:
            return None

        return {
            "evaluator_id": row["evaluator_id"],
            "sample_id": row["sample_id"],
            "display_order": row["display_order"],
            "completed": row["completed"],
            "scores": {
                "completeness": row["completeness_score"],
                "correctness": row["correctness_score"],
                "readability": row["readability_score"],
                "overall_quality": row["overall_quality"],
            },
            "overall_quality": row["overall_quality"],
            "deductions_json": row["deductions_json"],
            "detailed_comment": row["detailed_comment"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "active_seconds": row["active_seconds"],
            "paused_seconds": row["paused_seconds"],
            "pause_count": row["pause_count"],
            "reset_count": row["reset_count"],
            "llm_judge_opened": row["llm_judge_opened"],
            "llm_judge_first_opened_at": row["llm_judge_first_opened_at"],
            "llm_judge_open_count": row["llm_judge_open_count"],
            "llm_judge_visible_seconds": row["llm_judge_visible_seconds"],
        }

    def delete_evaluation(self, evaluator_id, sample_id):
        self._require_own_scope(evaluator_id, "Deleting another evaluator's evaluation")
        """Delete one evaluator's saved evaluation for a single sample.

        Used by the evaluator's Reset action to return a sample to the
        un-evaluated state. Snapshots the database first (the same safety net
        as the other destructive operations) and only when a row actually
        exists, so an idempotent Reset on an unsaved sample is a cheap no-op.
        Returns True when a row was removed, False when there was nothing to
        delete.
        """
        existing = self.conn.execute(
            "select 1 from evaluations where evaluator_id = ? and sample_id = ?",
            (evaluator_id, sample_id),
        ).fetchone()
        if existing is None:
            return False
        self.backup_database(
            f"delete_evaluation_{evaluator_id}_{pseudonymize_id(sample_id)}"
        )
        with self.conn:
            cursor = self.conn.execute(
                "delete from evaluations where evaluator_id = ? and sample_id = ?",
                (evaluator_id, sample_id),
            )
        return cursor.rowcount > 0

    def save_and_verify_evaluation(
        self,
        evaluator_id,
        sample_id,
        display_order,
        scores,
        deductions,
        detailed_comment,
        timer,
        llm_judge_view,
        completed=True,
    ):
        """Save an evaluation and verify the committed row matches requested values.

        Gated explicitly as well as through ``save_evaluation`` so the guard is
        visible on the method a caller actually reaches for.

        This proves row existence and logical value-match after SQLite commit. It does
        not prove physical fsync durability across storage or OS failures.
        """
        self._require_own_scope(
            evaluator_id, "Writing another evaluator's evaluation"
        )
        self.save_evaluation(
            evaluator_id=evaluator_id,
            sample_id=sample_id,
            display_order=display_order,
            scores=scores,
            deductions=deductions,
            detailed_comment=detailed_comment,
            timer=timer,
            llm_judge_view=llm_judge_view,
            completed=completed,
        )

        row = self.get_evaluation(evaluator_id, sample_id)
        if row is None:
            raise EvaluationSaveError(
                f"Evaluation save verification failed for evaluator_id={evaluator_id!r}, "
                f"sample_id={sample_id!r}: row missing"
            )

        expected_deductions = dict(deductions)
        expected_deductions["correctness"] = normalize_correctness_counts(
            expected_deductions.get("correctness", {})
        )
        comparisons = {
            "display_order": (display_order, row["display_order"]),
            "completed": (1 if completed else 0, row["completed"]),
            "scores.completeness": (
                scores["completeness"],
                row["scores"]["completeness"],
            ),
            "scores.correctness": (scores["correctness"], row["scores"]["correctness"]),
            "scores.readability": (scores["readability"], row["scores"]["readability"]),
            "scores.overall_quality": (
                scores.get("overall_quality"),
                row["scores"]["overall_quality"],
            ),
            "deductions": (
                expected_deductions,
                json.loads(row["deductions_json"]),
            ),
            "detailed_comment": (detailed_comment, row["detailed_comment"]),
            "started_at": (timer.get("started_at"), row["started_at"]),
            "ended_at": (timer.get("ended_at"), row["ended_at"]),
            "active_seconds": (timer.get("active_seconds", 0), row["active_seconds"]),
            "paused_seconds": (timer.get("paused_seconds", 0), row["paused_seconds"]),
            "pause_count": (timer.get("pause_count", 0), row["pause_count"]),
            "reset_count": (timer.get("reset_count", 0), row["reset_count"]),
            "llm_judge_opened": (
                1 if llm_judge_view.get("opened") else 0,
                row["llm_judge_opened"],
            ),
            "llm_judge_first_opened_at": (
                llm_judge_view.get("first_opened_at"),
                row["llm_judge_first_opened_at"],
            ),
            "llm_judge_open_count": (
                llm_judge_view.get("open_count", 0),
                row["llm_judge_open_count"],
            ),
            "llm_judge_visible_seconds": (
                llm_judge_view.get("visible_seconds", 0),
                row["llm_judge_visible_seconds"],
            ),
        }
        for field, (expected, actual) in comparisons.items():
            if actual != expected:
                raise EvaluationSaveError(
                    f"Evaluation save verification failed for evaluator_id={evaluator_id!r}, "
                    f"sample_id={sample_id!r}: {field} expected {expected!r} got {actual!r}"
                )

        return row

    def export_rows(self, evaluator_id=None):
        self._require_own_scope(evaluator_id, "Exporting another evaluator's results")
        query = """
            select
              evaluator_id,
              sample_id,
              display_order,
              completed,
              completeness_score,
              correctness_score,
              readability_score,
              overall_quality,
              deductions_json,
              detailed_comment,
              started_at,
              ended_at,
              active_seconds,
              paused_seconds,
              pause_count,
              reset_count,
              llm_judge_opened,
              llm_judge_first_opened_at,
              llm_judge_open_count,
              llm_judge_visible_seconds
            from evaluations
        """
        if evaluator_id is None:
            rows = self.conn.execute(
                query + " order by evaluator_id, display_order, sample_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                query + " where evaluator_id = ? order by display_order, sample_id",
                (evaluator_id,),
            ).fetchall()
        return [_flatten_export_row(row) for row in rows]

    def import_evaluations(self, rows):
        """Merge externally exported evaluations (parsed JSONL records) into this
        store. Used by the admin to aggregate per-evaluator result files.

        Records for samples not present in this database are skipped and counted.
        Unknown evaluators are auto-registered as approved evaluators so they
        appear as comparison columns. A record is also skipped (and counted) when
        its evaluator_id could not have been created in the app: it breaks the
        ID rules, differs from an existing account only by letter case, or names
        an admin account. Returns a summary dict.
        """
        # Writes rows for arbitrary evaluator_ids and creates accounts, so this
        # is strictly an admin aggregation step.
        self._require_admin("Importing evaluations")
        payloads = [_evaluation_import_payload(row) for row in rows]
        known_samples = {
            row["sample_id"]
            for row in self.conn.execute("select sample_id from samples").fetchall()
        }
        summary = {
            "imported": 0,
            "evaluators": [],
            "skipped_missing_sample": 0,
            "skipped_invalid_evaluator": 0,
            "created_evaluators": [],
        }
        for payload in payloads:
            if payload["sample_id"] not in known_samples:
                summary["skipped_missing_sample"] += 1
                continue
            outcome = self._ensure_imported_evaluator(payload["evaluator_id"])
            if outcome is None:
                summary["skipped_invalid_evaluator"] += 1
                continue
            if outcome == "created":
                summary["created_evaluators"].append(payload["evaluator_id"])
            self.save_evaluation(
                evaluator_id=payload["evaluator_id"],
                sample_id=payload["sample_id"],
                display_order=payload["display_order"],
                scores=payload["scores"],
                deductions=payload["deductions"],
                detailed_comment=payload["detailed_comment"],
                timer=payload["timer"],
                llm_judge_view=payload["llm_judge_view"],
                completed=payload["completed"],
            )
            summary["imported"] += 1
            if payload["evaluator_id"] not in summary["evaluators"]:
                summary["evaluators"].append(payload["evaluator_id"])
        return summary

    def _ensure_imported_evaluator(self, user_id):
        """Make sure an imported record's evaluator exists.

        Returns "existing" or "created", or None when the record must be
        skipped (see ``import_evaluations``). Imported IDs get the same rules as
        IDs created in the app, so an import cannot plant an account the
        dashboard would refuse -- or evaluations under an admin's name.
        """
        rows = self.conn.execute(
            "select user_id, role, status from users where lower(user_id) = lower(?)",
            (user_id,),
        ).fetchall()
        for row in rows:
            if (
                row["user_id"] == user_id
                and row["role"] == "evaluator"
                and row["status"] == "approved"
            ):
                return "existing"
        if rows or not USER_ID_PATTERN.fullmatch(str(user_id)):
            return None
        salt, password_hash = _hash_password(os.urandom(18).hex())
        with self.conn:
            self.conn.execute(
                """
                insert into users (
                  user_id, role, password_salt, password_hash, name, status, approved_at
                )
                values (?, 'evaluator', ?, ?, ?, 'approved', ?)
                """,
                (user_id, salt, password_hash, user_id, _now_iso()),
            )
        return "created"

    def completed_evaluation_details(self, evaluator_id):
        self._require_own_scope(evaluator_id, "Reading another evaluator's detail")
        rows = self.conn.execute(
            """
            select
              evaluations.evaluator_id,
              evaluations.sample_id,
              evaluations.display_order,
              evaluations.completed,
              evaluations.completeness_score,
              evaluations.correctness_score,
              evaluations.readability_score,
              evaluations.overall_quality,
              evaluations.deductions_json,
              evaluations.detailed_comment,
              evaluations.started_at,
              evaluations.ended_at,
              evaluations.active_seconds,
              evaluations.paused_seconds,
              evaluations.pause_count,
              evaluations.reset_count,
              evaluations.llm_judge_opened,
              evaluations.llm_judge_first_opened_at,
              evaluations.llm_judge_open_count,
              evaluations.llm_judge_visible_seconds,
              samples.discharge_note,
              samples.brief_hospital_course,
              samples.llm_judge
            from evaluations
            join samples
              on samples.sample_id = evaluations.sample_id
            where evaluations.evaluator_id = ?
              and evaluations.completed = 1
            order by evaluations.display_order, evaluations.sample_id
            """,
            (evaluator_id,),
        ).fetchall()
        return [_evaluation_detail_row(row) for row in rows]

    def sample_review_comparison_details(self):
        self._require_admin("Cross-evaluator review")
        samples = self.conn.execute(
            """
            select sample_id, discharge_note, brief_hospital_course, llm_judge
            from samples
            order by sample_id
            """
        ).fetchall()
        reviewers = self.list_users(role="evaluator")
        rows = self.conn.execute(
            """
            select
              evaluations.evaluator_id,
              evaluations.sample_id,
              evaluations.display_order,
              evaluations.completed,
              evaluations.completeness_score,
              evaluations.correctness_score,
              evaluations.readability_score,
              evaluations.overall_quality,
              evaluations.deductions_json,
              evaluations.detailed_comment,
              evaluations.started_at,
              evaluations.ended_at,
              evaluations.active_seconds,
              evaluations.paused_seconds,
              evaluations.pause_count,
              evaluations.reset_count,
              evaluations.llm_judge_opened,
              evaluations.llm_judge_first_opened_at,
              evaluations.llm_judge_open_count,
              evaluations.llm_judge_visible_seconds,
              samples.discharge_note,
              samples.brief_hospital_course,
              samples.llm_judge
            from evaluations
            join samples
              on samples.sample_id = evaluations.sample_id
            join users
              on users.user_id = evaluations.evaluator_id
             and users.role = 'evaluator'
             and users.status = 'approved'
            where evaluations.completed = 1
            order by evaluations.evaluator_id, evaluations.sample_id
            """
        ).fetchall()
        completed_by_reviewer_sample = {
            (row["evaluator_id"], row["sample_id"]): _evaluation_detail_row(row)
            for row in rows
        }

        comparison = []
        for index, sample in enumerate(samples, start=1):
            reviewer_rows = []
            for reviewer in reviewers:
                detail = completed_by_reviewer_sample.get((reviewer.user_id, sample["sample_id"]))
                if detail is None:
                    reviewer_rows.append(
                        {
                            "evaluator_id": reviewer.user_id,
                            "name": reviewer.name,
                            "completed": False,
                            "scores": None,
                            "deductions": None,
                            "detailed_comment": "",
                        }
                    )
                    continue

                reviewer_rows.append(
                    {
                        "evaluator_id": reviewer.user_id,
                        "name": reviewer.name,
                        "completed": True,
                        "scores": detail["scores"],
                        "deductions": detail["deductions"],
                        "detailed_comment": detail["detailed_comment"],
                    }
                )

            comparison.append(
                {
                    "sample_id": sample["sample_id"],
                    "display_order": index,
                    "sample_order": index,
                    "discharge_note": sample["discharge_note"],
                    "brief_hospital_course": sample["brief_hospital_course"],
                    "llm_judge": sample["llm_judge"],
                    "reviewers": reviewer_rows,
                }
            )
        return comparison

    def reset_evaluations(self):
        self._require_admin("Resetting evaluations")
        self.backup_database("reset_evaluations")
        with self.conn:
            self.conn.execute("delete from evaluations")

    def reset_all(self):
        """Factory reset the DB for a fresh round of data collection.

        Deletes every evaluation and every evaluator account. Samples (the
        dataset) and the admin account are kept, so the admin running this is
        not locked out mid-session and can create the new round's evaluator
        accounts from the dashboard straight away. A full backup is taken first.

        This is the one-click equivalent of shipping a clean database, without
        having to delete the file by hand. Returns a summary of what was removed.
        """
        self._require_admin("Full reset")
        self.backup_database("reset_all")
        removed = self.conn.execute(
            "select count(*) as n from evaluations"
        ).fetchone()["n"]
        removed_evaluators = self.conn.execute(
            "select count(*) as n from users where role = 'evaluator'"
        ).fetchone()["n"]
        with self.conn:
            self.conn.execute("delete from evaluations")
            self.conn.execute("delete from users where role = 'evaluator'")
        return {
            "evaluations_removed": removed,
            "evaluator_accounts_removed": removed_evaluators,
        }

    def progress_summary(self):
        self._require_admin("Reading cross-evaluator progress")
        total = self.conn.execute("select count(*) as count from samples").fetchone()["count"]
        completed = self.conn.execute(
            "select count(*) as count from evaluations where completed = 1"
        ).fetchone()["count"]
        return {"total_samples": total, "completed_evaluations": completed}

    def evaluator_progress(self):
        self._require_admin("Reading cross-evaluator progress")
        total = self.conn.execute("select count(*) as count from samples").fetchone()["count"]
        rows = self.conn.execute(
            """
            select
              users.user_id as evaluator_id,
              count(evaluations.sample_id) as completed
            from users
            left join evaluations
              on evaluations.evaluator_id = users.user_id
             and evaluations.completed = 1
            where users.role = 'evaluator'
              and users.status = 'approved'
            group by users.user_id
            order by users.user_id
            """
        ).fetchall()
        return [
            {
                "evaluator_id": row["evaluator_id"],
                "completed": row["completed"],
                "total": total,
            }
            for row in rows
        ]

    def evaluator_score_summary(self):
        self._require_admin("Reading cross-evaluator scores")
        total = self.conn.execute("select count(*) as count from samples").fetchone()["count"]
        rows = self.conn.execute(
            """
            select
              users.user_id as evaluator_id,
              count(evaluations.sample_id) as completed,
              avg(evaluations.completeness_score) as completeness_avg,
              avg(evaluations.correctness_score) as correctness_avg,
              avg(evaluations.readability_score) as readability_avg,
              avg(evaluations.overall_quality) as overall_quality_avg
            from users
            left join evaluations
              on evaluations.evaluator_id = users.user_id
             and evaluations.completed = 1
            where users.role = 'evaluator'
              and users.status = 'approved'
            group by users.user_id
            order by users.user_id
            """
        ).fetchall()
        return [
            {
                "evaluator_id": row["evaluator_id"],
                "completed": row["completed"],
                "total": total,
                "completeness_avg": _round_score(row["completeness_avg"]),
                "correctness_avg": _round_score(row["correctness_avg"]),
                "readability_avg": _round_score(row["readability_avg"]),
                "overall_quality_avg": _round_score(row["overall_quality_avg"]),
            }
            for row in rows
        ]


def _flatten_export_row(row):
    deductions = json.loads(row["deductions_json"])
    flat = dict(row)
    flat.pop("deductions_json", None)

    for key in COMPLETENESS_DEDUCTION_KEYS:
        flat[f"completeness_{key}"] = 1 if key in deductions.get("completeness", []) else 0
    correctness_counts = normalize_correctness_counts(deductions.get("correctness", {}))
    for key, count in correctness_counts.items():
        flat[f"correctness_{key}_count"] = count
    for key in ("long_sentence", "abbreviations", "duplicated", "formatting", "grammar"):
        flat[f"readability_{key}"] = 1 if key in deductions.get("readability", []) else 0

    return flat


def _evaluation_detail_row(row):
    deductions = json.loads(row["deductions_json"])
    correctness_counts = normalize_correctness_counts(deductions.get("correctness", {}))
    return {
        "evaluator_id": row["evaluator_id"],
        "sample_id": row["sample_id"],
        "display_order": row["display_order"],
        "completed": bool(row["completed"]),
        "discharge_note": row["discharge_note"],
        "brief_hospital_course": row["brief_hospital_course"],
        "llm_judge": row["llm_judge"],
        "scores": {
            "completeness": row["completeness_score"],
            "correctness": row["correctness_score"],
            "readability": row["readability_score"],
            "overall_quality": row["overall_quality"],
        },
        "deductions": {
            "completeness": deductions.get("completeness", []),
            "correctness": correctness_counts,
            "readability": deductions.get("readability", []),
        },
        "detailed_comment": row["detailed_comment"],
        "timer": {
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "active_seconds": row["active_seconds"],
            "paused_seconds": row["paused_seconds"],
            "pause_count": row["pause_count"],
            "reset_count": row["reset_count"],
        },
        "llm_judge_view": {
            "opened": bool(row["llm_judge_opened"]),
            "first_opened_at": row["llm_judge_first_opened_at"],
            "open_count": row["llm_judge_open_count"],
            "visible_seconds": row["llm_judge_visible_seconds"],
        },
    }


def _round_score(value):
    if value is None:
        return None
    return round(float(value), 2)


def restrict_file(path):
    """Make a file owner-only. Best effort: no-op where the OS disagrees."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


_restrict_file = restrict_file


def _restrict_dir(path):
    """Make a directory owner-only. Best effort: no-op where the OS disagrees."""
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _safe_backup_reason(reason):
    return "".join(c if c.isalnum() else "_" for c in reason)


_BACKUP_STAMP_RE = re.compile(r"-(\d{8}-\d{6}-\d{6})\.db$")


def _backup_stamp(filename):
    """Extract the trailing %Y%m%d-%H%M%S-%f stamp an auto-backup was named with.

    Returns the zero-padded string (lexically sortable == chronologically
    sortable) or None if the name has no such stamp.
    """
    m = _BACKUP_STAMP_RE.search(filename)
    return m.group(1) if m else None


def pseudonymize_id(value):
    """Short stable stand-in for a patient identifier in a filename or log line.

    sample_id may be a patient-linked identifier, so writing it into a backup filename or a failure log
    puts a patient identifier somewhere that leaks far more casually than file
    contents do: directory listings, backup indexes, screen shares, support
    tickets. The digest is stable, so an admin can still correlate a dump with a
    log line. It is deliberately not a de-identification claim -- the ID space is
    small enough to brute force if someone has the source list -- it just keeps
    identifiers out of incidental exposure.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return digest[:10]


def _hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        200_000,
    ).hex()
    return salt, digest


def _verify_password(password, salt, expected_hash):
    _, digest = _hash_password(password, salt)
    return hmac.compare_digest(digest, expected_hash)


def _now_iso():
    return datetime.now().astimezone().isoformat()

def _evaluation_import_payload(row):
    if not isinstance(row, dict):
        raise ValueError("Each evaluation record must be a JSON object")
    evaluator_id = row.get("evaluator_id")
    sample_id = row.get("sample_id")
    scores = row.get("scores")
    if not isinstance(evaluator_id, str) or not evaluator_id:
        raise ValueError("Evaluation record has missing or invalid evaluator_id")
    if not isinstance(sample_id, str) or not sample_id:
        raise ValueError("Evaluation record has missing or invalid sample_id")
    if not isinstance(scores, dict):
        raise ValueError("Evaluation record has missing or invalid scores")
    for key in ("completeness", "correctness", "readability"):
        if key not in scores:
            raise ValueError(f"Evaluation record scores missing '{key}'")
    deductions = row.get("deductions") or {}
    timer = row.get("timer") or {}
    llm = row.get("llm_judge_view") or {}
    completed = row.get("completed", False)
    llm_opened = llm.get("opened", False)
    if not isinstance(completed, bool):
        raise ValueError("Evaluation record completed must be a boolean")
    if not isinstance(llm_opened, bool):
        raise ValueError("Evaluation record llm_judge_view.opened must be a boolean")
    return {
        "evaluator_id": evaluator_id,
        "sample_id": sample_id,
        "display_order": row.get("display_order", 0),
        "scores": {
            "completeness": scores["completeness"],
            "correctness": scores["correctness"],
            "readability": scores["readability"],
            "overall_quality": scores.get("overall_quality"),
        },
        "deductions": {
            "completeness": list(deductions.get("completeness", []) or []),
            "correctness": dict(deductions.get("correctness", {}) or {}),
            "readability": list(deductions.get("readability", []) or []),
        },
        "detailed_comment": row.get("detailed_comment", "") or "",
        "timer": {
            "started_at": timer.get("started_at"),
            "ended_at": timer.get("ended_at"),
            "active_seconds": timer.get("active_seconds", 0),
            "paused_seconds": timer.get("paused_seconds", 0),
            "pause_count": timer.get("pause_count", 0),
            "reset_count": timer.get("reset_count", 0),
        },
        "llm_judge_view": {
            "opened": llm_opened,
            "first_opened_at": llm.get("first_opened_at"),
            "open_count": llm.get("open_count", 0),
            "visible_seconds": llm.get("visible_seconds", 0),
        },
        "completed": completed,
    }
