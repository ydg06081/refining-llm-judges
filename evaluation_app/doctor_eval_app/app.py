import sys
from pathlib import Path

from doctor_eval_app.sample_data import (
    ADDITIONAL_SAMPLE_CSVS,
    DEMO_SAMPLES,
    REAL_SAMPLE_CSV_NAME,
    load_samples_from_csv,
)
from doctor_eval_app.storage import DoctorEvalStore


class StartupError(RuntimeError):
    """A startup problem the launcher should surface to the user, not a crash."""


def default_base_dir():
    """Resolve a stable, writable data directory.

    When packaged with PyInstaller (``sys.frozen``), the launch directory
    (``Path.cwd()``) is unpredictable and often read-only (e.g. System32 or
    Program Files on Windows). Anchoring to the executable's own folder keeps
    the portable ``storage/``, ``data/`` and ``exports/`` layout next to the
    .exe regardless of where the user launches it from, so evaluations are
    never written to a throwaway location.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def create_store(base_dir=None):
    base_dir = Path(base_dir or default_base_dir())
    store = DoctorEvalStore(base_dir / "storage" / "doctor_eval.db")
    store.initialize()
    return store


def ensure_demo_data(store):
    if store.progress_summary()["total_samples"] > 0:
        return
    store.import_samples(DEMO_SAMPLES)


def ensure_sample_data(store, base_dir=None, allow_demo=False):
    """Load the evaluation samples into the store.

    Two steps. The first seeds the configured dataset into an *empty* store and
    refuses to silently seed the 4-sample demo set when the dataset file is
    missing: because seeding is one-shot (guarded on total_samples > 0) a
    silent fallback would stick permanently -- an evaluator could complete a
    whole session against 4 placeholder samples and nothing downstream would
    flag it. A missing dataset raises so the launcher can tell the user, and
    the demo set is opt-in (tests).

    The second step runs on *every* launch, including over a database with
    finished evaluations in it, and imports any later top-up round whose CSV has
    been dropped into data/ and registered in ADDITIONAL_SAMPLE_CSVS, without
    disturbing the order of work already done.
    """
    base_dir = Path(base_dir or default_base_dir())
    _ensure_first_batch(store, base_dir, allow_demo)
    return import_additional_batches(store, base_dir)


def _ensure_first_batch(store, base_dir, allow_demo):
    real_csv = base_dir / "data" / REAL_SAMPLE_CSV_NAME

    if store.progress_summary()["total_samples"] > 0:
        _refuse_database_from_another_round(store, real_csv)
        return

    if real_csv.exists():
        store.import_samples(load_samples_from_csv(real_csv), batch=1)
        return

    if allow_demo:
        store.import_samples(DEMO_SAMPLES, batch=1)
        return

    raise StartupError(
        "평가 데이터 파일을 찾을 수 없습니다.\n\n"
        f"프로그램과 같은 폴더 안에 data\\{REAL_SAMPLE_CSV_NAME} 파일이 "
        "있어야 합니다.\n"
        "doctor-eval 폴더를 통째로 복사했는지 확인한 뒤 다시 실행하세요.\n"
        "문제가 계속되면 관리자에게 문의하세요."
    )


def _refuse_database_from_another_round(store, real_csv):
    """Stop when this folder's database belongs to a different evaluation round.

    Sample import is guarded on an empty database, so a database left over from
    a previous round silently prevents the current dataset from loading: the
    evaluator is shown the *old* round, every sample already ticked off, with no
    error and nothing to do. That is what happens when someone copies a new
    build over their existing folder instead of using a fresh one, and it is
    invisible from the outside -- the app looks like it started fine.

    A database that shares even one sample_id with the configured dataset is the
    same round (or a round being topped up) and passes. Sharing none of them
    means the wrong database, so say so rather than showing stale work.
    """
    if not real_csv.exists():
        # No dataset to compare against. A missing dataset on a populated
        # database is not this function's problem -- the app can still run the
        # samples it already holds.
        return

    try:
        dataset_ids = {sample.sample_id for sample in load_samples_from_csv(real_csv)}
    except (OSError, ValueError):
        # A broken or unreadable dataset is reported by the import path with a
        # better message; do not pre-empt it with a confusing mismatch error.
        return

    if not dataset_ids:
        return

    stored_ids = {
        sample_id
        for sample_ids in store.sample_batches().values()
        for sample_id in sample_ids
    }
    if stored_ids & dataset_ids:
        return

    raise StartupError(
        "이 폴더에 남아 있는 저장 데이터가 이번 평가 회차와 맞지 않습니다.\n\n"
        "이전 회차의 데이터가 들어 있어서 이번 평가 문항을 불러올 수 없습니다.\n"
        "관리자에게 새 폴더를 받아서 실행해 주세요.\n\n"
        "이전 폴더는 지우지 마시고 그대로 보관해 주세요. "
        "이전에 하신 평가 결과가 그 안에 들어 있습니다."
    )


def import_additional_batches(store, base_dir=None):
    """Import each later round's CSV that is present and not already loaded.

    Idempotent: a round whose samples are all in the database is skipped, so a
    relaunch imports nothing and the evaluator's ordering never moves. Returns
    the rounds actually imported, as (filename, batch, count).
    """
    base_dir = Path(base_dir or default_base_dir())
    known_batch = {
        sample_id: batch
        for batch, sample_ids in store.sample_batches().items()
        for sample_id in sample_ids
    }

    imported = []
    for filename, batch in ADDITIONAL_SAMPLE_CSVS:
        csv_path = base_dir / "data" / filename
        if not csv_path.exists():
            continue

        try:
            samples = load_samples_from_csv(csv_path)
        except (OSError, ValueError) as exc:
            raise StartupError(
                f"추가 평가 데이터 파일을 읽을 수 없습니다: data\\{filename}\n\n"
                f"{exc}\n\n"
                "관리자에게 파일을 다시 받으세요."
            ) from exc

        # A sample_id from this round that is already filed under a different
        # batch means the wrong file was shipped. Importing it would renumber a
        # round the evaluator has already worked through, so stop instead.
        misfiled = sorted(
            sample.sample_id
            for sample in samples
            if known_batch.get(sample.sample_id, batch) != batch
        )
        if misfiled:
            raise StartupError(
                f"추가 평가 데이터 파일이 기존 데이터와 맞지 않습니다: data\\{filename}\n\n"
                f"이미 다른 회차에 포함된 샘플입니다: {misfiled[:5]}\n"
                "이 파일을 그대로 사용하면 이미 끝난 평가의 순서가 바뀝니다.\n"
                "관리자에게 문의하세요."
            )

        new_samples = [s for s in samples if s.sample_id not in known_batch]
        if not new_samples:
            continue

        # Never grow a round someone is already partway through: order inside a
        # batch is derived from its members, so adding one would move samples
        # already shown. A fully-new round has no evaluations and is fine.
        if store.batch_has_evaluations(batch):
            raise StartupError(
                f"이미 평가가 진행된 회차에는 샘플을 추가할 수 없습니다 (batch {batch}).\n\n"
                f"data\\{filename} 파일이 배포된 것과 다른 것 같습니다.\n"
                "관리자에게 문의하세요."
            )

        store.import_samples(new_samples, batch=batch)
        known_batch.update({s.sample_id: batch for s in new_samples})
        imported.append((filename, batch, len(new_samples)))

    return imported


def _writability_probe(base_dir):
    """Confirm the app can write next to itself before it opens SQLite.

    A frozen app anchors storage/ to the exe's own folder. If that folder is
    read-only (Program Files, a USB stick, a locked network share, or a
    OneDrive folder mid-sync), the first write is deep inside SQLite and would
    otherwise surface as a raw traceback dialog with no window. Probe first so
    we can say something a clinician can act on.
    """
    base_dir = Path(base_dir)
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        probe = base_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise StartupError(
            "이 폴더에 파일을 저장할 수 없습니다.\n\n"
            "doctor-eval 폴더를 바탕화면이나 문서 폴더로 복사한 뒤 "
            "다시 실행하세요.\n"
            "(USB, 읽기 전용 위치, 동기화 중인 클라우드 폴더에서는 "
            "평가 결과가 저장되지 않습니다.)"
        ) from exc


def main():
    from doctor_eval_app.ui import run_app

    base_dir = default_base_dir()

    # Build the QApplication up front so a startup failure can be shown in a
    # dialog rather than dying as an invisible traceback (console=False).
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ModuleNotFoundError:
        print("PySide6 is not installed. Run: pip install -r requirements.txt")
        return 1
    app = QApplication.instance() or QApplication([])

    try:
        _writability_probe(base_dir)
        store = create_store(base_dir)
        ensure_sample_data(store, base_dir)
    except StartupError as exc:
        QMessageBox.critical(None, "실행할 수 없습니다", str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort, never crash silently
        QMessageBox.critical(
            None,
            "실행할 수 없습니다",
            "프로그램을 시작하는 중 문제가 발생했습니다.\n"
            "doctor-eval 폴더를 바탕화면으로 복사한 뒤 다시 실행하고, "
            "문제가 계속되면 관리자에게 아래 내용을 전달하세요.\n\n"
            f"{type(exc).__name__}: {exc}",
        )
        return 1

    raise SystemExit(run_app(store))
