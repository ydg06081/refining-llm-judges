"""Produce the distribution CSV: only the columns the app actually reads.

A source dataset may carry many more columns than the app needs, including
direct patient identifiers the application never touches -- patient numbers,
age, sex, admission/discharge dates, and full narrative sections. Shipping the
whole file puts re-identifiable data on every evaluator's laptop for no reason.

The app reads exactly three columns (sample_data.REQUIRED_CSV_COLUMNS), plus
llm_judge if it is ever present. This writes a trimmed copy containing only
those, which imports identically (verified) while carrying nothing the app does
not use.

Usage:
    python scripts/make_dist_csv.py <src_csv> <dest_csv>
Exits non-zero (and writes nothing) if a required column is missing, so a build
fails loudly rather than shipping a broken or demo-only dataset.
"""

import csv
import sys
from pathlib import Path

# Import the single source of truth for which columns the app consumes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from doctor_eval_app.sample_data import (  # noqa: E402
    SAMPLE_ID_COLUMN,
    missing_csv_columns,
    sample_id_column,
)

# Order the app's importer is happy with; llm_judge is optional. The identifier
# is always written as sample_id, even when the source uses a legacy name.
OUTPUT_COLUMNS = [SAMPLE_ID_COLUMN, "discharge_note_no_bhc", "brief_hospital_course"]
OPTIONAL_COLUMNS = ["llm_judge"]


def make_dist_csv(src, dest):
    src, dest = Path(src), Path(dest)
    with src.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        present = set(reader.fieldnames or [])
        missing = missing_csv_columns(present)
        if missing:
            raise SystemExit(
                f"ERROR: source CSV {src} is missing required column(s): "
                f"{sorted(missing)}. Refusing to build a broken dataset."
            )
        columns = list(OUTPUT_COLUMNS)
        columns += [c for c in OPTIONAL_COLUMNS if c in present]
        id_column = sample_id_column(present)

        rows = list(reader)
        for row in rows:
            row[SAMPLE_ID_COLUMN] = row.get(id_column, "")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})

    dropped = sorted(present - set(columns) - {id_column})
    print(
        f"Wrote {len(rows)} rows x {len(columns)} columns to {dest}\n"
        f"Kept: {columns}\n"
        f"Dropped {len(dropped)} unused column(s): {dropped}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python scripts/make_dist_csv.py <src_csv> <dest_csv>")
    make_dist_csv(sys.argv[1], sys.argv[2])
