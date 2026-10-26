import csv
import json
from pathlib import Path


COMPLETENESS_KEYS = (
    "missing_reason",
    "missing_condition",
    "missing_treatment",
    "course_not_documented",
)
CORRECTNESS_KEYS = ("false_information", "causal_error")
READABILITY_KEYS = (
    "long_sentence",
    "abbreviations",
    "duplicated",
    "formatting",
    "grammar",
)
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def write_csv(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_safe_csv_row(_clean_row(row)) for row in rows]
    fieldnames = _fieldnames(rows)

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(rows, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(_structured_row(row), ensure_ascii=False) + "\n")


def _clean_row(row):
    cleaned = dict(row)
    for key in ("discharge_note", "brief_hospital_course", "llm_judge"):
        cleaned.pop(key, None)
    return cleaned


def _safe_csv_row(row):
    return {key: _safe_csv_cell(value) for key, value in row.items()}


def _safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _fieldnames(rows):
    preferred = [
        "evaluator_id",
        "sample_id",
        "display_order",
        "completed",
        "completeness_score",
        "correctness_score",
        "readability_score",
        "overall_quality",
        "completeness_missing_reason",
        "completeness_missing_condition",
        "completeness_missing_treatment",
        "completeness_course_not_documented",
        "correctness_false_information_count",
        "correctness_causal_error_count",
        "readability_long_sentence",
        "readability_abbreviations",
        "readability_duplicated",
        "readability_formatting",
        "readability_grammar",
        "detailed_comment",
        "started_at",
        "ended_at",
        "active_seconds",
        "paused_seconds",
        "pause_count",
        "reset_count",
        "llm_judge_opened",
        "llm_judge_first_opened_at",
        "llm_judge_open_count",
        "llm_judge_visible_seconds",
    ]
    available = set()
    for row in rows:
        available.update(row.keys())
    return [key for key in preferred if key in available] + sorted(available - set(preferred))


def _structured_row(row):
    row = _clean_row(row)
    return {
        "evaluator_id": row["evaluator_id"],
        "sample_id": row["sample_id"],
        "display_order": row["display_order"],
        "completed": bool(row["completed"]),
        "scores": {
            "completeness": row["completeness_score"],
            "correctness": row["correctness_score"],
            "readability": row["readability_score"],
            "overall_quality": row.get("overall_quality"),
        },
        "deductions": {
            "completeness": [
                key for key in COMPLETENESS_KEYS if row.get(f"completeness_{key}")
            ],
            "correctness": {
                key: int(row.get(f"correctness_{key}_count", 0) or 0)
                for key in CORRECTNESS_KEYS
            },
            "readability": [
                key for key in READABILITY_KEYS if row.get(f"readability_{key}")
            ],
        },
        "detailed_comment": row.get("detailed_comment", ""),
        "timer": {
            "started_at": row.get("started_at"),
            "ended_at": row.get("ended_at"),
            "active_seconds": row.get("active_seconds", 0),
            "paused_seconds": row.get("paused_seconds", 0),
            "pause_count": row.get("pause_count", 0),
            "reset_count": row.get("reset_count", 0),
        },
        "llm_judge_view": {
            "opened": bool(row.get("llm_judge_opened", 0)),
            "first_opened_at": row.get("llm_judge_first_opened_at"),
            "open_count": row.get("llm_judge_open_count", 0),
            "visible_seconds": row.get("llm_judge_visible_seconds", 0),
        },
    }
