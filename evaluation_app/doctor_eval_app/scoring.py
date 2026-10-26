METRIC_DEDUCTIONS = {
    "completeness": {
        "missing_reason",
        "missing_condition",
        "missing_treatment",
        "course_not_documented",
    },
    "correctness": {
        "false_information",
        "causal_error",
    },
    "readability": {
        "long_sentence",
        "abbreviations",
        "duplicated",
        "formatting",
        "grammar",
    },
}
CORRECTNESS_DEDUCTION_KEYS = ("false_information", "causal_error")


def score_from_deductions(metric, deduction_keys):
    if metric not in METRIC_DEDUCTIONS:
        raise ValueError(f"Unknown metric: {metric}")

    if metric == "correctness":
        counts = normalize_correctness_counts(deduction_keys)
        return max(1, 5 - sum(counts.values()))

    valid_keys = METRIC_DEDUCTIONS[metric]
    selected = set(deduction_keys)
    unknown = selected - valid_keys
    if unknown:
        raise ValueError(f"Unknown deduction key for {metric}: {sorted(unknown)}")

    return max(1, 5 - len(selected))


def normalize_correctness_counts(deductions):
    valid_keys = set(CORRECTNESS_DEDUCTION_KEYS)
    counts = dict.fromkeys(CORRECTNESS_DEDUCTION_KEYS, 0)

    if isinstance(deductions, dict):
        unknown = set(deductions) - valid_keys
        if unknown:
            raise ValueError(f"Unknown deduction key for correctness: {sorted(unknown)}")
        for key, value in deductions.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("Correctness deduction counts must be nonnegative integers")
            counts[key] = value
        return counts

    selected = list(deductions)
    unknown = set(selected) - valid_keys
    if unknown:
        raise ValueError(f"Unknown deduction key for correctness: {sorted(unknown)}")
    for key in selected:
        counts[key] += 1
    return counts


def validate_overall_quality(value):
    if not isinstance(value, int) or value < 1 or value > 5:
        raise ValueError("Overall Quality must be an integer from 1 to 5")
    return value
