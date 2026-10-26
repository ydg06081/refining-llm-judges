import tempfile
import unittest
from pathlib import Path

import _accounts  # noqa: F401  (test fixture accounts)
from doctor_eval_app.app import create_store, ensure_sample_data
from doctor_eval_app.sample_data import REAL_SAMPLE_CSV_NAME, load_samples_from_csv


class SampleDataTest(unittest.TestCase):
    def test_real_sample_csv_name_points_at_a_dataset_that_actually_loads(self):
        """The configured dataset must exist and be importable.

        The repository ships a synthetic demo set (scripts/make_sample_dataset.py):
        3 fictional cases, each with a complete and a deliberately flawed Brief
        Hospital Course. Pinning the literal filename only catches a typo; this
        also catches pointing at a file that is missing, unreadable, or short a
        required column -- the failure that would otherwise surface as a refused
        launch on an evaluator's laptop.
        """
        self.assertEqual(REAL_SAMPLE_CSV_NAME, "sample_dataset.csv")

        dataset = Path(__file__).resolve().parent.parent / "data" / REAL_SAMPLE_CSV_NAME
        self.assertTrue(dataset.is_file(), f"dataset missing: {dataset}")

        samples = load_samples_from_csv(dataset)
        self.assertEqual(len(samples), 6)
        self.assertEqual(len({s.sample_id for s in samples}), 6)
        # Each case appears twice, under neutral A/B ids, with the same note and
        # a different summary.
        notes = {}
        for sample in samples:
            self.assertRegex(sample.sample_id, r"^demo_\d{2}_[AB]$")
            notes.setdefault(sample.sample_id[:-2], []).append(sample)
        self.assertEqual(len(notes), 3)
        for pair in notes.values():
            self.assertEqual(len(pair), 2)
            self.assertEqual(pair[0].discharge_note, pair[1].discharge_note)
            self.assertNotEqual(
                pair[0].brief_hospital_course, pair[1].brief_hospital_course
            )
        for sample in samples:
            self.assertTrue(sample.sample_id)
            self.assertTrue(sample.discharge_note.strip())
            self.assertTrue(sample.brief_hospital_course.strip())
            # The evaluator must not be shown the answer inside the note.
            self.assertNotIn("Brief Hospital Course:", sample.discharge_note)

    def test_load_samples_from_csv_maps_real_eval_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            path.write_text(
                (
                    "sample_id,discharge_note_no_bhc,brief_hospital_course\n"
                    "1001,\"note without bhc\",\"brief course\"\n"
                ),
                encoding="utf-8",
            )

            samples = load_samples_from_csv(path)

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].sample_id, "1001")
        self.assertEqual(samples[0].discharge_note, "note without bhc")
        self.assertEqual(samples[0].brief_hospital_course, "brief course")
        self.assertEqual(samples[0].llm_judge, "")

    def test_load_samples_from_csv_accepts_legacy_pact_id_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            path.write_text(
                "pact_id,discharge_note_no_bhc,brief_hospital_course\n"
                "1001,note,course\n",
                encoding="utf-8",
            )

            samples = load_samples_from_csv(path)

        self.assertEqual([s.sample_id for s in samples], ["1001"])

    def test_sample_id_column_wins_over_legacy_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            path.write_text(
                "pact_id,sample_id,discharge_note_no_bhc,brief_hospital_course\n"
                "old,new,note,course\n",
                encoding="utf-8",
            )

            samples = load_samples_from_csv(path)

        self.assertEqual([s.sample_id for s in samples], ["new"])

    def test_load_samples_from_csv_rejects_missing_required_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "samples.csv"
            path.write_text("sample_id,brief_hospital_course\n1001,brief\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_samples_from_csv(path)

    def test_ensure_sample_data_prefers_real_csv_over_demo_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            data_dir = base_dir / "data"
            data_dir.mkdir()
            (data_dir / REAL_SAMPLE_CSV_NAME).write_text(
                (
                    "sample_id,discharge_note_no_bhc,brief_hospital_course\n"
                    "1001,\"note without bhc\",\"brief course\"\n"
                ),
                encoding="utf-8",
            )
            store = create_store(base_dir)
            try:
                ensure_sample_data(store, base_dir)
                samples = store.list_samples_for_evaluator("evaluator_a")
            finally:
                store.close()

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["sample_id"], "1001")


class ImportSurvivesJudgePanelRemovalTest(unittest.TestCase):
    """Removing the LLM-as-Judge panel was a UI-only change. Both import paths --
    the sample CSV read at startup and the admin's evaluator-results JSONL merge --
    must keep working unchanged, including for files produced before the change."""

    def test_sample_csv_import_keeps_optional_llm_judge_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            data_dir = base_dir / "data"
            data_dir.mkdir()
            (data_dir / REAL_SAMPLE_CSV_NAME).write_text(
                (
                    "sample_id,discharge_note_no_bhc,brief_hospital_course,llm_judge\n"
                    "1001,\"note\",\"course\",\"judge verdict\"\n"
                ),
                encoding="utf-8",
            )
            store = create_store(base_dir)
            try:
                ensure_sample_data(store, base_dir)
                detail = store.list_samples_for_evaluator("evaluator_a")[0]
            finally:
                store.close()

        # The column is still read and still stored; only the evaluator's view
        # of it is gone.
        self.assertEqual(detail["brief_hospital_course"], "course")
        self.assertEqual(detail["llm_judge"], "judge verdict")

    def test_sample_csv_import_without_llm_judge_column_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            data_dir = base_dir / "data"
            data_dir.mkdir()
            (data_dir / REAL_SAMPLE_CSV_NAME).write_text(
                (
                    "sample_id,discharge_note_no_bhc,brief_hospital_course\n"
                    "1001,\"note\",\"course\"\n"
                ),
                encoding="utf-8",
            )
            store = create_store(base_dir)
            try:
                ensure_sample_data(store, base_dir)
                detail = store.list_samples_for_evaluator("evaluator_a")[0]
            finally:
                store.close()

        self.assertEqual(detail["llm_judge"], "")

    def test_admin_import_accepts_pre_change_results_with_judge_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_dir = Path(tmp)
            data_dir = base_dir / "data"
            data_dir.mkdir()
            (data_dir / REAL_SAMPLE_CSV_NAME).write_text(
                (
                    "sample_id,discharge_note_no_bhc,brief_hospital_course\n"
                    "1001,\"note\",\"course\"\n"
                ),
                encoding="utf-8",
            )
            store = create_store(base_dir)
            try:
                ensure_sample_data(store, base_dir)

                # A results file exported by an older build, where the panel
                # existed and the evaluator opened it.
                old_build_record = {
                    "evaluator_id": "evaluator_a",
                    "sample_id": "1001",
                    "display_order": 1,
                    "completed": True,
                    "scores": {
                        "completeness": 4,
                        "correctness": 5,
                        "readability": 3,
                        "overall_quality": 4,
                    },
                    "deductions": {
                        "completeness": [],
                        "correctness": {},
                        "readability": [],
                    },
                    "detailed_comment": "from the old build",
                    "timer": {"active_seconds": 120},
                    "llm_judge_view": {
                        "opened": True,
                        "first_opened_at": "2026-06-15T09:05:00+09:00",
                        "open_count": 3,
                        "visible_seconds": 88,
                    },
                }
                # A results file exported by the current build, where there is
                # no panel to open.
                new_build_record = dict(old_build_record)
                new_build_record["evaluator_id"] = "evaluator_b"
                new_build_record["detailed_comment"] = "from the current build"
                new_build_record["llm_judge_view"] = {
                    "opened": False,
                    "first_opened_at": None,
                    "open_count": 0,
                    "visible_seconds": 0,
                }
                # And one with no llm_judge_view key at all.
                bare_record = dict(old_build_record)
                bare_record["evaluator_id"] = "evaluator_c"
                bare_record.pop("llm_judge_view")

                summary = store.import_evaluations(
                    [old_build_record, new_build_record, bare_record]
                )
                self.assertEqual(summary["imported"], 3)

                rows = {row["evaluator_id"]: row for row in store.export_rows()}
            finally:
                store.close()

        # Pre-change exposure numbers survive the merge untouched.
        self.assertEqual(rows["evaluator_a"]["llm_judge_opened"], 1)
        self.assertEqual(rows["evaluator_a"]["llm_judge_open_count"], 3)
        self.assertEqual(rows["evaluator_a"]["llm_judge_visible_seconds"], 88)
        self.assertEqual(
            rows["evaluator_a"]["llm_judge_first_opened_at"],
            "2026-06-15T09:05:00+09:00",
        )
        # Post-change rows record "never shown", and a missing key behaves the same.
        for evaluator in ("evaluator_b", "evaluator_c"):
            self.assertEqual(rows[evaluator]["llm_judge_opened"], 0)
            self.assertEqual(rows[evaluator]["llm_judge_open_count"], 0)
            self.assertEqual(rows[evaluator]["llm_judge_visible_seconds"], 0)
            self.assertIsNone(rows[evaluator]["llm_judge_first_opened_at"])
        # The scores themselves came through for all three.
        for evaluator in ("evaluator_a", "evaluator_b", "evaluator_c"):
            self.assertEqual(rows[evaluator]["completeness_score"], 4)
            self.assertEqual(rows[evaluator]["correctness_score"], 5)
            self.assertEqual(rows[evaluator]["readability_score"], 3)


if __name__ == "__main__":
    unittest.main()
