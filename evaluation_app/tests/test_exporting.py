import csv
import json
import tempfile
import unittest
from pathlib import Path

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app.exporting import write_csv, write_jsonl


class ExportingTest(unittest.TestCase):
    def test_write_csv_uses_flat_columns_and_excludes_raw_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"

            write_csv([_row()], path)

            with path.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_id"], "sample_001")
        self.assertEqual(
            list(rows[0]),
            [
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
            ],
        )
        self.assertEqual(rows[0]["readability_score"], "3")
        self.assertIn("completeness_missing_condition", rows[0])
        self.assertEqual(rows[0]["correctness_false_information_count"], "5")
        self.assertEqual(rows[0]["correctness_causal_error_count"], "1")
        self.assertNotIn("discharge_note", rows[0])
        self.assertNotIn("brief_hospital_course", rows[0])
        self.assertNotIn("llm_judge", rows[0])

    def test_write_jsonl_writes_one_structured_object_per_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"

            write_jsonl([_row()], path)

            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["sample_id"], "sample_001")
        self.assertEqual(
            set(payload),
            {
                "evaluator_id",
                "sample_id",
                "display_order",
                "completed",
                "scores",
                "deductions",
                "detailed_comment",
                "timer",
                "llm_judge_view",
            },
        )
        self.assertEqual(payload["scores"]["readability"], 3)
        self.assertEqual(
            payload["deductions"]["correctness"],
            {"false_information": 5, "causal_error": 1},
        )
        self.assertEqual(payload["timer"]["active_seconds"], 180)
        self.assertNotIn("discharge_note", payload)

    def test_write_csv_neutralizes_spreadsheet_formula_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            row = _row()
            row["sample_id"] = "=cmd|/C calc!A0"
            row["detailed_comment"] = "+SUM(1,1)"

            write_csv([row], path)

            with path.open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))

        self.assertEqual(rows[0]["sample_id"], "'=cmd|/C calc!A0")
        self.assertEqual(rows[0]["detailed_comment"], "'+SUM(1,1)")

    def test_write_jsonl_preserves_formula_like_strings_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.jsonl"
            row = _row()
            row["sample_id"] = "=cmd|/C calc!A0"
            row["detailed_comment"] = "+SUM(1,1)"

            write_jsonl([row], path)

            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(payload["sample_id"], "=cmd|/C calc!A0")
        self.assertEqual(payload["detailed_comment"], "+SUM(1,1)")


def _row():
    return {
        "evaluator_id": "evaluator_a",
        "sample_id": "sample_001",
        "display_order": 1,
        "completed": 1,
        "completeness_score": 4,
        "correctness_score": 5,
        "readability_score": 3,
        "overall_quality": 4,
        "detailed_comment": "usable with edits",
        "started_at": "2026-06-08T09:00:00+09:00",
        "ended_at": "2026-06-08T09:03:00+09:00",
        "active_seconds": 180,
        "paused_seconds": 0,
        "pause_count": 0,
        "reset_count": 0,
        "llm_judge_opened": 1,
        "llm_judge_first_opened_at": "2026-06-08T09:01:00+09:00",
        "llm_judge_open_count": 1,
        "llm_judge_visible_seconds": 40,
        "completeness_missing_reason": 0,
        "completeness_missing_condition": 1,
        "completeness_missing_treatment": 0,
        "completeness_course_not_documented": 0,
        "correctness_false_information_count": 5,
        "correctness_causal_error_count": 1,
        "readability_long_sentence": 0,
        "readability_abbreviations": 0,
        "readability_duplicated": 1,
        "readability_formatting": 1,
        "readability_grammar": 0,
    }


if __name__ == "__main__":
    unittest.main()
