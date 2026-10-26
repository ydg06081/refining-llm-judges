import unittest

from doctor_eval_app.scoring import (
    score_from_deductions,
    validate_overall_quality,
)


class ScoringTest(unittest.TestCase):
    def test_metric_score_starts_at_five_and_deducts_one_per_item(self):
        self.assertEqual(score_from_deductions("completeness", []), 5)
        self.assertEqual(
            score_from_deductions(
                "completeness",
                ["missing_reason", "missing_condition"],
            ),
            3,
        )

    def test_metric_score_never_drops_below_one(self):
        self.assertEqual(
            score_from_deductions(
                "readability",
                [
                    "long_sentence",
                    "abbreviations",
                    "duplicated",
                    "formatting",
                    "grammar",
                ],
            ),
            1,
        )

    def test_correctness_score_uses_counts_but_caps_deduction_at_four(self):
        self.assertEqual(
            score_from_deductions(
                "correctness",
                {"false_information": 5, "causal_error": 2},
            ),
            1,
        )

    def test_unknown_deduction_key_is_rejected(self):
        with self.assertRaises(ValueError):
            score_from_deductions("correctness", ["not_a_real_key"])

    def test_correctness_count_values_must_be_nonnegative_integers(self):
        for value in (-1, 1.5, "2", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    score_from_deductions("correctness", {"false_information": value})

    def test_overall_quality_accepts_one_to_five(self):
        for value in range(1, 6):
            self.assertEqual(validate_overall_quality(value), value)

    def test_overall_quality_rejects_values_outside_one_to_five(self):
        for value in (0, 6, "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_overall_quality(value)


if __name__ == "__main__":
    unittest.main()
