import csv
import tempfile
import unittest
from pathlib import Path
import _accounts  # noqa: F401  (test fixture accounts)


ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def test_windows_packaging_files_exist(self):
        for relative_path in (
            "run_doctor_eval.py",
            "doctor-eval.spec",
            "scripts/build_windows.bat",
            "requirements-build.txt",
        ):
            with self.subTest(path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_windows_build_script_uses_pyinstaller_spec(self):
        script = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")

        self.assertIn("python -m venv .venv", script)
        self.assertIn(".venv\\Scripts\\python.exe -m pip install -r requirements.txt", script)
        self.assertIn(".venv\\Scripts\\python.exe -m pip install -r requirements-build.txt", script)
        self.assertIn(".venv\\Scripts\\python.exe -m PyInstaller doctor-eval.spec", script)

    def test_spec_builds_portable_folder_named_doctor_eval(self):
        spec = (ROOT / "doctor-eval.spec").read_text(encoding="utf-8")

        self.assertIn("run_doctor_eval.py", spec)
        self.assertIn("name='doctor-eval'", spec)
        self.assertIn("console=False", spec)

    def test_build_ships_trimmed_dataset_not_the_whole_data_folder(self):
        script = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")
        # The trimmer runs and its output feeds the dist; the whole data\
        # folder is never copied.
        self.assertIn("scripts\\make_dist_csv.py", script)
        self.assertNotIn('xcopy "data"', script)
        # And the build refuses to ship without the dataset (no silent demo).
        self.assertIn("evaluation dataset missing", script)
        self.assertTrue((ROOT / "scripts" / "make_dist_csv.py").is_file())

    def test_build_never_ships_a_database(self):
        """The dist folder must go out with an empty storage\\.

        A post-build test launch would leave test accounts and evaluations in
        the shipped database. And because sample import is guarded on an empty
        database, an evaluator who receives one never imports the dataset --
        they get whatever samples that database holds, with no error.
        """
        script = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")

        self.assertIn("del /q", script)
        self.assertIn("dist\\doctor-eval\\storage\\doctor_eval.db", script)

    def test_build_ships_the_dataset_the_app_is_configured_to_load(self):
        """The build and sample_data must not drift apart.

        If build_windows.bat still trims last round's CSV while
        REAL_SAMPLE_CSV_NAME points at this round's, the dist folder ships a
        dataset the app refuses to load, and the failure only shows up on an
        evaluator's laptop.
        """
        from doctor_eval_app.sample_data import REAL_SAMPLE_CSV_NAME

        script = (ROOT / "scripts" / "build_windows.bat").read_text(encoding="utf-8")
        self.assertIn(f"data\\{REAL_SAMPLE_CSV_NAME}", script)
        self.assertIn(
            f"dist\\doctor-eval\\data\\{REAL_SAMPLE_CSV_NAME}", script
        )
        self.assertTrue((ROOT / "data" / REAL_SAMPLE_CSV_NAME).is_file())

    def test_make_dist_csv_emits_only_the_columns_the_app_reads(self):
        from scripts.make_dist_csv import make_dist_csv
        from doctor_eval_app.sample_data import REQUIRED_CSV_COLUMNS

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.csv"
            src.write_text(
                "pact_id,age,sex,patient_no,discharge_note_no_bhc,brief_hospital_course\n"
                "1001,70,M,X0001,note,course\n",
                encoding="utf-8",
            )
            dest = Path(tmp) / "dist.csv"
            make_dist_csv(src, dest)
            with dest.open(encoding="utf-8") as f:
                header = next(csv.reader(f))
        # A legacy pact_id source is written out under the current name.
        self.assertEqual(set(header), REQUIRED_CSV_COLUMNS)
        self.assertIn("sample_id", header)
        self.assertNotIn("pact_id", header)
        for identifier in ("age", "sex", "patient_no"):
            self.assertNotIn(identifier, header)


if __name__ == "__main__":
    unittest.main()
