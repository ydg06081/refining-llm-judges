"""Test-only fixture accounts.

The shipped app seeds no accounts: on first launch the user creates the admin
account, and the admin creates evaluator accounts from the dashboard. Most of
the suite, however, exercises multi-evaluator flows against a fresh database,
so importing this module makes every new test database start with these fixed
accounts (via ``DoctorEvalStore.seed_accounts``).

The admin fixture is flagged must_change_password so the forced-rotation flow
stays covered; tests rotate it to ADMIN_TEST_PASSWORD before logging in.

Import it at the top of any test module that relies on these accounts:

    import _accounts  # noqa: F401
"""

from doctor_eval_app.storage import DoctorEvalStore

ADMIN_ID = "admin"
ADMIN_INITIAL_PASSWORD = "admin"
EVALUATOR_PASSWORDS = {
    "evaluator_a": "pass1234a",
    "evaluator_b": "pass1234b",
    "evaluator_c": "pass1234c",
}

TEST_ACCOUNTS = (
    (ADMIN_ID, ADMIN_INITIAL_PASSWORD, "admin", "Admin", True),
    ("evaluator_a", EVALUATOR_PASSWORDS["evaluator_a"], "evaluator", "Evaluator A", False),
    ("evaluator_b", EVALUATOR_PASSWORDS["evaluator_b"], "evaluator", "Evaluator B", False),
    ("evaluator_c", EVALUATOR_PASSWORDS["evaluator_c"], "evaluator", "Evaluator C", False),
)

# What the released app seeds, captured before the override below so a test can
# pin it (test_security_hardening.SeedingTest.test_shipped_app_seeds_no_accounts).
SHIPPED_SEED_ACCOUNTS = DoctorEvalStore.seed_accounts

DoctorEvalStore.seed_accounts = TEST_ACCOUNTS
