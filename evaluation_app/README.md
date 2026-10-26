# doctor-eval

A desktop application for clinician evaluation of **Brief Hospital Course (BHC)**
summaries. Each evaluator reads a discharge note (with its BHC removed) next to
a candidate BHC and scores the summary with a deduction-based rubric. An admin
manages evaluator accounts, monitors progress, compares evaluators side by side
and exports the results.

Built with PySide6 (Qt) and SQLite. Runs offline; all data stays in a local
folder next to the application.

> **No real patient data is included.** `data/sample_dataset.csv` contains
> synthetic, fictional cases written for demonstration only
> (see [Sample data](#sample-data)).

## Quick start

From the repository root (the app keeps `data/`, `storage/` and `exports/` in
the directory it is started from):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_doctor_eval.py
```

Tested with Python 3.14 and PySide6 6.11 on macOS; the packaged build targets
Windows (see [Building a Windows package](#building-a-windows-package)).

## Accounts

The application ships with **no accounts**.

1. **First launch** — with no admin account in the database, the app opens on
   an *admin setup* screen. Choose the admin ID and password (at least 8
   characters). This screen is shown only once.
2. **Sign in** — the login screen only signs in; there is no self-registration.
3. **Evaluator accounts** — in the admin dashboard, *Evaluator Accounts →
   Add evaluator* creates an account with an ID, display name and password.
   The account can sign in immediately. The same section offers *Reset
   password* and *Delete* (deleting an evaluator also deletes their
   evaluations).

Account IDs are 3–32 characters (letters, digits, `_`, `.`, `-`), start with a
letter or digit, must not end in `.` or `-`, and are unique regardless of
letter case. Passwords are stored as salted PBKDF2-SHA256 hashes.

There is no password recovery for the admin account: keep it safe. Deleting
`storage/doctor_eval.db` resets the application to the first-launch state
(and deletes all evaluations).

## Evaluating

Evaluators see their samples in a per-evaluator randomised order (stable across
sessions). Scoring inputs unlock when the evaluator presses **Start**, which
also starts a timer; **End** saves the evaluation. Work in progress is
autosaved as a draft and recovered after a crash.

Rubric (each metric starts at 5 and loses one point per deduction, minimum 1):

| Metric | Deductions |
|---|---|
| Completeness | reason for hospitalization missing · discharge condition missing · essential treatment missing · treatment course not documented |
| Correctness | false information (counted per element) · incorrect explanation of causal relationships (counted) |
| Readability | excessive sentence length · unexplained abbreviations · repeated content · lack of structured formatting · grammatical errors |
| Overall quality | 1 complete rewriting required … 5 acceptable as written |

Evaluators can add a free-text comment and export their own results as JSONL.

## Admin dashboard

- Overall and per-evaluator progress, and a score summary per evaluator.
- **Review** — read-only side-by-side comparison of all evaluators per sample.
- **Export CSV / Export JSONL** — all evaluations. CSV cells are protected
  against spreadsheet formula injection.
- **Import Results** — merge JSONL files exported by evaluators working on
  separate machines. Records for unknown samples, or with an evaluator ID the
  app would not accept, are skipped and reported.
- **Reset** deletes all evaluations; **Full Reset** deletes all evaluations and
  all evaluator accounts (samples and the admin account are kept). Both take a
  backup in `storage/backups/` first.

## Data

### Input format

The dataset is a UTF-8 CSV in `data/` with the columns:

| Column | Content |
|---|---|
| `sample_id` | unique sample identifier |
| `discharge_note_no_bhc` | discharge note shown to the evaluator, **without** its BHC |
| `brief_hospital_course` | the BHC being evaluated |
| `llm_judge` | optional; stored with the sample but not shown |

A CSV whose identifier column is named `pact_id` is also accepted. The file
loaded on first launch is set by `REAL_SAMPLE_CSV_NAME` in
`doctor_eval_app/sample_data.py` (default `sample_dataset.csv`). To run your
own study, replace the CSV (or point that constant at another file in `data/`)
**before** the first launch: samples are imported only into an empty database.

To compare several summaries of the same note, add one row per summary with
the same note and distinct IDs (the sample data uses `demo_01_A`, `demo_01_B`,
…). Avoid IDs that reveal which system produced a summary.

### Sample batches

To add more samples to a study already in progress without disturbing the
order or numbering of completed work, put a CSV containing **only the new
samples** in `data/` and register it in `ADDITIONAL_SAMPLE_CSVS` in
`doctor_eval_app/sample_data.py` with the next batch number, e.g.
`(("round2.csv", 2),)`. It is imported on the next launch and appended after
the existing samples for every evaluator.

### Sample data

`data/sample_dataset.csv` holds three fictional cases (pneumonia with COPD
exacerbation, acute appendicitis, new-onset atrial fibrillation), each with two
BHCs: a complete one (`_A`) and one with deliberate omissions, factual errors
and readability problems (`_B`). Regenerate it with:

```bash
python scripts/make_sample_dataset.py
```

### Where data is stored

| Path | Content |
|---|---|
| `storage/doctor_eval.db` | SQLite database: samples, accounts, evaluations |
| `storage/backups/` | automatic backups before destructive actions |
| `exports/` | default location for exported results |

`storage/`, `exports/` and every file in `data/` except the sample dataset are
git-ignored. **Never commit a database or a dataset containing real patient
data.**

## Building a Windows package

On Windows, with Python on `PATH`:

```bat
scripts\build_windows.bat
```

The script sets everything up itself: it creates `.venv\` in the repository
root, installs `requirements.txt` and `requirements-build.txt` (PyInstaller),
and builds with `doctor-eval.spec`. The result is a portable folder
`dist\doctor-eval\` containing `doctor-eval.exe`; no Python is needed on the
machines that run it. Give the whole folder to evaluators.

The script expects the dataset at `data\sample_dataset.csv`. If you configure a
different file name in `REAL_SAMPLE_CSV_NAME`, update the script too. The build
copies only the columns the app reads (`scripts/make_dist_csv.py`) and never
ships a database, so the recipient starts at the first-launch admin setup. To hand out a ready-to-use
folder instead, launch it once, create the accounts, then copy the folder.
Optional Authenticode signing is described in the script.

When upgrading an existing installation, copy everything **except**
`storage\` over the old folder — `storage\` holds the saved evaluations.

## Tests

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=. python -m unittest discover -s tests
```

`tests/_accounts.py` defines fixed fixture accounts for the test suite only;
the application itself seeds none.

## License

[MIT](LICENSE)
