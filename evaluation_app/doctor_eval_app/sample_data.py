import csv
from pathlib import Path

from doctor_eval_app.models import Sample


# The dataset loaded into an empty database on first launch (batch 1).
#
# The file shipped in this repository, data/sample_dataset.csv, contains
# SYNTHETIC, fictional discharge notes written for demonstration only -- no real
# patient data. To run your own study, replace it with your own CSV (same
# columns) or point REAL_SAMPLE_CSV_NAME at a different file in data/.
#
# Columns: sample_id (the sample identifier), discharge_note_no_bhc (the
# discharge note shown to the evaluator, without its Brief Hospital Course) and
# brief_hospital_course (the summary being evaluated). An optional llm_judge
# column is read and stored but not shown. Older CSVs that name the identifier
# column pact_id are still accepted.
REAL_SAMPLE_CSV_NAME = "sample_dataset.csv"
SAMPLE_ID_COLUMN = "sample_id"
LEGACY_SAMPLE_ID_COLUMNS = ("pact_id",)
REQUIRED_CSV_COLUMNS = {SAMPLE_ID_COLUMN, "discharge_note_no_bhc", "brief_hospital_course"}

# Later top-up rounds, as (filename, batch). Each entry is an *additive* file
# holding only that round's new samples -- never a replacement for the file
# before it. The batch number is pinned to the filename rather than computed
# (e.g. max(batch)+1) so re-running the app, or importing the files in a
# different order, can never land the same round in a different batch and
# reshuffle work an evaluator has already completed.
#
# To add a round: drop the CSV in data/ (columns: sample_id,
# discharge_note_no_bhc, brief_hospital_course), add a line here with the next
# batch number, and ship it alongside the exe. See README "Sample batches".
# Empty by default: the dataset ships as a single batch.
ADDITIONAL_SAMPLE_CSVS = ()


# Built-in placeholder samples, used only when explicitly requested (tests, or
# ensure_sample_data(allow_demo=True)). Like data/sample_dataset.csv they are
# SYNTHETIC: short fictional cases written for demonstration, not derived from
# any patient record.
DEMO_SAMPLES = [
    Sample(
        sample_id="synthetic_01",
        discharge_note=(
            "Fictional patient admitted with fever and productive cough. Chest "
            "radiograph showed a right lower lobe infiltrate. IV antibiotics and "
            "supplemental oxygen were started.\n\n"
            "Discharge medications and follow-up plan were documented."
        ),
        brief_hospital_course=(
            "Treated for right lower lobe pneumonia with IV antibiotics, then switched "
            "to oral therapy. Oxygen was weaned to room air. Discharged home in stable "
            "condition with primary care follow-up in one week."
        ),
        llm_judge=(
            "LLM Judge (synthetic): Reason for admission and treatment course are "
            "present. Discharge condition is stated."
        ),
    ),
    Sample(
        sample_id="synthetic_02",
        discharge_note=(
            "Fictional patient presented with right upper quadrant pain after meals. "
            "Ultrasound showed gallstones without duct dilation. Managed with bowel "
            "rest and analgesia; surgery consulted."
        ),
        brief_hospital_course="Pain improved. Discharge instructions were provided.",
        llm_judge=(
            "LLM Judge (synthetic): Very brief; omits the imaging findings and the "
            "surgical plan."
        ),
    ),
    Sample(
        sample_id="synthetic_03",
        discharge_note=(
            "Fictional patient admitted with acute kidney injury after poor oral "
            "intake. IV fluids were given and an NSAID was stopped."
        ),
        brief_hospital_course=(
            "Creatinine returned toward baseline with IV fluids after the NSAID was "
            "held. Discharged with instructions to avoid NSAIDs and a repeat lab check "
            "in one week."
        ),
        llm_judge="LLM Judge (synthetic): Mostly complete. Readability is acceptable.",
    ),
    Sample(
        sample_id="synthetic_04",
        discharge_note=(
            "Fictional patient admitted after a mechanical fall at home. Head CT "
            "showed no acute findings. Physical therapy evaluated gait and balance."
        ),
        brief_hospital_course=(
            "No neurologic change during the stay. Cleared by physical therapy and "
            "discharged home with a walker."
        ),
        llm_judge=(
            "LLM Judge (synthetic): Course and disposition are present; fall-risk "
            "follow-up is not mentioned."
        ),
    ),
]


def sample_id_column(fieldnames):
    """The column holding the sample identifier, or None if there is none.

    Prefers ``sample_id``; falls back to a legacy name such as ``pact_id``.
    """
    present = set(fieldnames or [])
    for column in (SAMPLE_ID_COLUMN, *LEGACY_SAMPLE_ID_COLUMNS):
        if column in present:
            return column
    return None


def missing_csv_columns(fieldnames):
    """Required columns absent from ``fieldnames``, honouring legacy id names."""
    present = set(fieldnames or [])
    id_column = sample_id_column(present)
    if id_column:
        present.add(SAMPLE_ID_COLUMN)
    return REQUIRED_CSV_COLUMNS - present


def load_samples_from_csv(path):
    path = Path(path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = missing_csv_columns(reader.fieldnames)
        if missing:
            raise ValueError(f"Missing required CSV columns: {sorted(missing)}")
        id_column = sample_id_column(reader.fieldnames)

        samples = []
        for index, row in enumerate(reader, start=1):
            sample_id = (row.get(id_column) or f"sample_{index:03d}").strip()
            samples.append(
                Sample(
                    sample_id=sample_id,
                    discharge_note=row["discharge_note_no_bhc"],
                    brief_hospital_course=row["brief_hospital_course"],
                    llm_judge=row.get("llm_judge", ""),
                    metadata={"source_row": index},
                )
            )
        return samples
