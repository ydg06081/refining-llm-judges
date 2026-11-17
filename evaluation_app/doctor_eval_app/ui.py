from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path

from doctor_eval_app.exporting import write_csv, write_jsonl
from doctor_eval_app.scoring import CORRECTNESS_DEDUCTION_KEYS, score_from_deductions
from doctor_eval_app.storage import (
    COMPLETENESS_DEDUCTION_KEYS,
    MIN_PASSWORD_LENGTH,
    pseudonymize_id,
    restrict_file,
)


NOTE_MIN_HEIGHT = 480
COURSE_MIN_HEIGHT = 120
COURSE_MAX_HEIGHT = 220
SAMPLE_NAV_LABELS = {"previous": "←", "next": "→"}
NOTICE_DEFAULT = "Start를 눌러야 점수와 자유서술 입력이 열립니다."
NOTICE_EDIT = "Edit 버튼을 누르면 점수와 자유서술을 편집할 수 있고, 시간도 이어서 측정됩니다."
EVALUATOR_CARD_MIN_WIDTH = 300
EVALUATOR_CARD_MIN_HEIGHT = 240
CORRECTNESS_COUNT_MAX = 999
CARD_BG = "#fbfcfd"
CARD_BORDER = "#d9e0e7"
SAFE_FILENAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
ACCENT = "#1d6f8f"
TEXT = "#1d232b"
TEXT_MUTED = "#667280"
PANEL = "#eef2f6"
RADIUS = 8
GAP = 16


def card_qss():
    return f"""
    QFrame {{
      background: {CARD_BG};
      border: 1px solid {CARD_BORDER};
      border-radius: {RADIUS}px;
    }}
    QLabel {{
      border: 0;
      background: transparent;
    }}
    """


def progress_qss():
    return f"""
    QProgressBar {{
      border: 1px solid {CARD_BORDER};
      border-radius: 7px;
      height: 24px;
      text-align: center;
      background: {PANEL};
    }}
    QProgressBar::chunk {{
      border-radius: 6px;
      background: {ACCENT};
    }}
    """


def button_qss(variant="secondary"):
    if variant == "primary":
        return f"""
        QPushButton {{
          background: {ACCENT};
          color: white;
          border: 0;
          border-radius: 6px;
          padding: 8px 14px;
          font-weight: 700;
        }}
        QPushButton:hover {{ background: #175f78; }}
        QPushButton:pressed {{ background: #124a5d; }}
        """
    if variant == "danger":
        return """
        QPushButton {
          background: #fff4f2;
          color: #b42318;
          border: 1px solid #f3b8b1;
          border-radius: 6px;
          padding: 8px 14px;
          font-weight: 700;
        }
        QPushButton:hover { background: #ffe7e3; }
        QPushButton:pressed { background: #ffd6cf; }
        """
    return f"""
    QPushButton {{
      background: #ffffff;
      color: {TEXT};
      border: 1px solid {CARD_BORDER};
      border-radius: 6px;
      padding: 8px 14px;
      font-weight: 700;
    }}
    QPushButton:hover {{ background: #f3f7fb; }}
    QPushButton:pressed {{ background: #e7edf3; }}
    """


def _safe_filename_part(value):
    text = str(value)
    cleaned = "".join(char if char in SAFE_FILENAME_CHARS else "-" for char in text)
    return cleaned.strip(".-") or "unknown"


def _coerce_int(value, default=0):
    """int() that never raises. A recovery dump with a bad numeric value must
    not be able to crash the form it is being restored into."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _atomic_write_text(path, text):
    """Write a file so a crash mid-write cannot leave a half-written dump.

    The recovery scanner treats an unparseable dump as corrupt; a plain
    write_text truncates then fills, so power loss between the two leaves
    exactly such a file. Write a sibling temp file, flush+fsync it, then rename
    over the target (atomic on POSIX and on Windows for same-directory renames).
    """
    import os as _os

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        _os.fsync(handle.fileno())
    _os.replace(tmp, path)


def apply_card(widget):
    shape = getattr(type(widget), "StyledPanel", None)
    if shape is None:
        shape = getattr(getattr(type(widget), "Shape", None), "StyledPanel", None)
    if shape is not None:
        widget.setFrameShape(shape)
    widget.setStyleSheet(card_qss())
    return widget
CORRECTNESS_COUNT_LABELS = {
    "false_information": "False information per element",
    "causal_error": "Incorrect explanation of causal relationships",
}
EVALUATION_DEDUCTION_OPTIONS = {
    "completeness": [
        ("missing_reason", "Not containing reason for hospitalization"),
        ("missing_condition", "Not containing discharge condition"),
        ("missing_treatment", "Not containing essential treatment"),
        ("course_not_documented", "Treatment course not documented"),
    ],
    "correctness": [
        ("false_information", "False information per element"),
        ("causal_error", "Incorrect explanation of causal relationships"),
    ],
    "readability": [
        ("long_sentence", "Excessive sentence length impairing readability"),
        ("abbreviations", "Unexplained abbreviations or acronyms"),
        ("duplicated", "Repeated or duplicated content"),
        ("formatting", "Lack of structured formatting"),
        ("grammar", "Presence of grammatical errors impairing readability"),
    ],
}
OVERALL_QUALITY_OPTIONS = [
    (1, "Complete rewriting required"),
    (2, "Major revision required"),
    (3, "Moderate revision required"),
    (4, "Minor revision required"),
    (5, "Acceptable as written"),
]
ADMIN_DEDUCTION_LABELS = {
    "completeness": {
        "missing_reason": "Reason missing",
        "missing_condition": "Condition missing",
        "missing_treatment": "Treatment missing",
        "course_not_documented": "Course not documented",
    },
    "correctness": {
        "false_information": "False information",
        "causal_error": "Causal relationship error",
    },
    "readability": {
        "long_sentence": "Long sentence",
        "abbreviations": "Unexplained abbreviation",
        "duplicated": "Repeated or duplicated",
        "formatting": "Poor structure/formatting",
        "grammar": "Grammar issue",
    },
}
ADMIN_OVERALL_QUALITY_OPTIONS = [
    (1, "Rewrite required"),
    (2, "Major revision"),
    (3, "Moderate revision"),
    (4, "Minor revision"),
    (5, "Acceptable"),
]



def run_app(store, clock=None):
    try:
        from PySide6.QtCore import QTimer, Qt
        from PySide6.QtWidgets import (
            QApplication,
            QCheckBox,
            QComboBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QFrame,
            QGridLayout,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QListWidgetItem,
            QMainWindow,
            QMessageBox,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSizePolicy,
            QSplitter,
            QSpinBox,
            QStackedWidget,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ModuleNotFoundError:
        print("PySide6 is not installed. Run: pip install -r requirements.txt")
        return 1

    now = clock or (lambda: datetime.now().astimezone())

    def _input_qss():
        return f"""
        QLineEdit {{
          border: 1px solid {CARD_BORDER};
          border-radius: 6px;
          padding: 8px 10px;
          background: #ffffff;
          color: {TEXT};
        }}
        QLineEdit:focus {{
          border: 1px solid {ACCENT};
        }}
        """

    class LoginView(QWidget):
        def __init__(self, on_login):
            super().__init__()
            self.on_login = on_login
            self.account = QLineEdit()
            self.account.setPlaceholderText("Account ID")
            self.password = QLineEdit()
            self.password.setPlaceholderText("Password")
            self.password.setEchoMode(QLineEdit.Password)
            input_qss = _input_qss()
            self.account.setMinimumHeight(34)
            self.account.setStyleSheet(input_qss)
            self.password.setMinimumHeight(34)
            self.password.setStyleSheet(input_qss)
            self.error_label = QLabel("")
            self.error_label.setStyleSheet(f"color: #b42318; font-size: 12px;")
            self.error_label.setWordWrap(True)
            self.error_label.hide()
            self.show_password = QCheckBox("Show password")
            self.show_password.stateChanged.connect(self.toggle_password_visibility)

            layout = QVBoxLayout(self)
            self.setStyleSheet(f"background: {PANEL};")
            layout.setAlignment(Qt.AlignCenter)
            layout.setContentsMargins(24, 24, 24, 24)

            card = QFrame()
            card.setMaximumWidth(520)
            apply_card(card)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(28, 24, 28, 24)
            card_layout.setSpacing(14)
            title = QLabel("Doctor Evaluation")
            title.setStyleSheet(f"font-size: 28px; font-weight: 800; color: {TEXT};")
            subtitle = QLabel("Sign in with the account issued by your admin")
            subtitle.setStyleSheet(f"font-size: 13px; color: {TEXT_MUTED};")
            card_layout.addWidget(title)
            card_layout.addWidget(subtitle)

            form = QFormLayout()
            form.setVerticalSpacing(10)
            form.addRow("Account", self.account)
            form.addRow("Password", self.password)
            form.addRow("", self.show_password)
            form.addRow("", self.error_label)
            card_layout.addLayout(form)

            login_btn = QPushButton("Login")
            login_btn.setMinimumHeight(36)
            login_btn.setStyleSheet(button_qss("primary"))
            login_btn.clicked.connect(self.try_login)
            # Enter in either field submits, as on any sign-in form.
            self.account.returnPressed.connect(self.try_login)
            self.password.returnPressed.connect(self.try_login)
            card_layout.addWidget(login_btn)
            layout.addWidget(card)

        def prefill(self, account):
            self.account.setText(account or "")
            self.password.clear()
            self.clear_error()
            self.password.setFocus()

        def toggle_password_visibility(self):
            self.password.setEchoMode(
                QLineEdit.Normal if self.show_password.isChecked() else QLineEdit.Password
            )

        def show_error(self, message):
            self.error_label.setText(message)
            self.error_label.show()

        def clear_error(self):
            self.error_label.clear()
            self.error_label.hide()


        def try_login(self):
            account = self.account.text().strip()
            password = self.password.text()
            if not account or not password:
                self.show_error("Account and password are required.")
                (self.account if not account else self.password).setFocus()
                return

            user = store.authenticate(account, password)
            if not user:
                reason = store.login_block_reason(account)
                if reason in ("pending", "rejected"):
                    self.show_error("This account is not active. Contact your admin.")
                    return
                self.show_error("Account or password is incorrect.")
                return
            self.clear_error()
            self.on_login(user)

    class PasswordChangeDialog(QDialog):
        """Blocking password rotation for an account flagged
        must_change_password. Cancelling returns to login rather than letting
        the session continue on the old password.

        The admin dashboard reuses it (with its own title and notice) to set a
        new password for an evaluator who has forgotten theirs.
        """

        def __init__(self, user, parent=None, title=None, notice=None):
            super().__init__(parent)
            self.user = user
            self.setWindowTitle(title or "Password change required")
            self.setModal(True)

            self.password = QLineEdit()
            self.password.setEchoMode(QLineEdit.Password)
            self.password_check = QLineEdit()
            self.password_check.setEchoMode(QLineEdit.Password)
            self.error_label = QLabel()
            self.error_label.setStyleSheet("color: #b3261e;")
            self.error_label.setWordWrap(True)
            self.error_label.hide()

            layout = QVBoxLayout(self)
            notice_label = QLabel(
                notice
                or (
                    f"Account '{user.user_id}' must set a new password "
                    "before continuing."
                )
            )
            notice_label.setWordWrap(True)
            layout.addWidget(notice_label)

            form = QFormLayout()
            form.addRow("New password", self.password)
            form.addRow("Confirm new password", self.password_check)
            layout.addLayout(form)
            layout.addWidget(self.error_label)

            buttons = QDialogButtonBox(
                QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            )
            buttons.accepted.connect(self.submit)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def show_error(self, message):
            self.error_label.setText(message)
            self.error_label.show()

        def submit(self):
            password = self.password.text()
            if password != self.password_check.text():
                self.show_error("Passwords do not match.")
                return
            try:
                changed = store.set_password(self.user.user_id, password)
            except ValueError as exc:
                self.show_error(str(exc))
                return
            if not changed:
                self.show_error("Could not change the password.")
                return
            self.accept()

    def _account_fields(parent_layout, show_name=True):
        """ID / name / password / confirm inputs shared by the first-run admin
        setup screen and the admin's "Add evaluator" dialog."""
        fields = {}
        fields["account"] = QLineEdit()
        fields["account"].setPlaceholderText("3-32 chars: letters, digits, _ . -")
        fields["name"] = QLineEdit()
        fields["name"].setPlaceholderText("Optional display name")
        fields["password"] = QLineEdit()
        fields["password"].setEchoMode(QLineEdit.Password)
        fields["password"].setPlaceholderText(f"At least {MIN_PASSWORD_LENGTH} characters")
        fields["password_check"] = QLineEdit()
        fields["password_check"].setEchoMode(QLineEdit.Password)
        for widget in fields.values():
            widget.setMinimumHeight(34)
            widget.setStyleSheet(_input_qss())

        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("ID", fields["account"])
        if show_name:
            form.addRow("Name", fields["name"])
        form.addRow("Password", fields["password"])
        form.addRow("Confirm password", fields["password_check"])
        parent_layout.addLayout(form)
        return fields

    class AdminSetupView(QWidget):
        """First-launch screen: create the admin account.

        The app ships with no accounts. Until an admin exists this screen is
        shown instead of the login screen; once the admin is created it is never
        shown again (``store.create_initial_admin`` refuses a second admin).
        """

        def __init__(self, on_done):
            super().__init__()
            self.on_done = on_done
            self.setStyleSheet(f"background: {PANEL};")
            layout = QVBoxLayout(self)
            layout.setAlignment(Qt.AlignCenter)
            layout.setContentsMargins(24, 24, 24, 24)

            card = QFrame()
            card.setMaximumWidth(560)
            apply_card(card)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(28, 24, 28, 24)
            card_layout.setSpacing(14)
            title = QLabel("Create admin account")
            title.setStyleSheet(f"font-size: 26px; font-weight: 800; color: {TEXT};")
            subtitle = QLabel(
                "First launch: choose the ID and password of the admin account. "
                "The admin creates evaluator accounts from the dashboard."
            )
            subtitle.setWordWrap(True)
            subtitle.setStyleSheet(f"font-size: 13px; color: {TEXT_MUTED};")
            card_layout.addWidget(title)
            card_layout.addWidget(subtitle)

            fields = _account_fields(card_layout)
            self.account = fields["account"]
            self.name = fields["name"]
            self.password = fields["password"]
            self.password_check = fields["password_check"]

            self.error_label = QLabel("")
            self.error_label.setStyleSheet("color: #b42318; font-size: 12px;")
            self.error_label.setWordWrap(True)
            self.error_label.hide()
            card_layout.addWidget(self.error_label)

            create_btn = QPushButton("Create admin account")
            create_btn.setMinimumHeight(36)
            create_btn.setStyleSheet(button_qss("primary"))
            create_btn.clicked.connect(self.submit)
            self.password_check.returnPressed.connect(self.submit)
            card_layout.addWidget(create_btn)
            layout.addWidget(card)

        def show_error(self, message):
            self.error_label.setText(message)
            self.error_label.show()

        def submit(self):
            try:
                user = store.create_initial_admin(
                    user_id=self.account.text(),
                    password=self.password.text(),
                    password_check=self.password_check.text(),
                    name=self.name.text(),
                )
            except PermissionError:
                # Another window (or an earlier run) already created the admin:
                # this screen is obsolete, so hand over to the login screen
                # instead of leaving a button that can only fail.
                self.password.clear()
                self.password_check.clear()
                QMessageBox.information(
                    self,
                    "Admin account exists",
                    "An admin account has already been created. Sign in with it.",
                )
                self.on_done(None)
                return
            except ValueError as exc:
                self.show_error(str(exc))
                return
            self.password.clear()
            self.password_check.clear()
            QMessageBox.information(
                self,
                "Admin account created",
                f"Admin account '{user.user_id}' was created.\n"
                "Sign in with it to open the admin dashboard.",
            )
            self.on_done(user.user_id)

    class CreateEvaluatorDialog(QDialog):
        """Admin dashboard: issue an evaluator account (ID, name, password).

        Replaces self-service signup; the account can log in immediately.
        """

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Add evaluator account")
            self.setModal(True)
            self.setMinimumWidth(420)
            self.created_user = None

            layout = QVBoxLayout(self)
            fields = _account_fields(layout)
            self.account = fields["account"]
            self.name = fields["name"]
            self.password = fields["password"]
            self.password_check = fields["password_check"]

            self.error_label = QLabel()
            self.error_label.setStyleSheet("color: #b3261e;")
            self.error_label.setWordWrap(True)
            self.error_label.hide()
            layout.addWidget(self.error_label)

            buttons = QDialogButtonBox(
                QDialogButtonBox.Ok | QDialogButtonBox.Cancel
            )
            buttons.button(QDialogButtonBox.Ok).setText("Create")
            buttons.accepted.connect(self.submit)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def show_error(self, message):
            self.error_label.setText(message)
            self.error_label.show()

        def submit(self):
            try:
                self.created_user = store.create_evaluator_account(
                    user_id=self.account.text(),
                    password=self.password.text(),
                    password_check=self.password_check.text(),
                    name=self.name.text(),
                )
            except (ValueError, PermissionError) as exc:
                self.show_error(str(exc))
                return
            self.accept()

    class EvaluatorView(QWidget):
        def __init__(self, user, on_logout):
            super().__init__()
            self.user = user
            self.samples = store.list_samples_for_evaluator(user.user_id)
            # Open on the first sample still to do, not always on #1. Once a
            # top-up round is added, an evaluator who finished the first batch
            # would otherwise land on a completed sample and have to scroll past
            # all of them to reach the new work.
            self.current_index = next(
                (
                    index
                    for index, sample in enumerate(self.samples)
                    if not sample["completed"]
                ),
                0,
            )
            self.started_at = None
            self.active_started_at = None
            self.elapsed_seconds = 0
            self.paused_seconds = 0
            self.pause_started_at = None
            self.pause_count = 0
            self.reset_count = 0
            self.llm_open_count = 0
            self.llm_first_opened_at = None
            self.llm_visible_seconds = 0
            self.llm_last_opened = None
            self._last_unsaved_dump_path = None
            self._last_save_error = None
            # sample_id -> recovered dump payload awaiting re-entry into the form,
            # and sample_id -> [dump file paths] to delete once re-saved.
            self._recovery_dumps = {}
            self._recovery_files = {}
            self._draft_path = None

            self.timer = QTimer(self)
            self.timer.setInterval(1000)
            self.timer.timeout.connect(self.tick)
            # Periodic draft of in-progress work. Without it, everything typed
            # between Start and End lives only in widget memory, so a crash or
            # power loss mid-evaluation loses the whole read+score with no trace.
            # The draft is written as an ordinary unsaved-*.json dump the normal
            # recovery flow picks up on next login.
            self.autosave_timer = QTimer(self)
            self.autosave_timer.setInterval(10000)
            self.autosave_timer.timeout.connect(self._autosave_draft)

            root = QVBoxLayout(self)
            top = QHBoxLayout()
            title = QLabel(f"Doctor Evaluation · {user.user_id} · shuffled order")
            title.setStyleSheet("font-weight: 700;")
            top.addWidget(title)
            top.addStretch()
            self.progress_label = QLabel("0 / 0 완료")
            self.progress_label.setStyleSheet(f"color: {TEXT_MUTED}; font-weight: 700;")
            top.addWidget(self.progress_label)
            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setMaximumWidth(220)
            self.progress_bar.setStyleSheet(progress_qss())
            top.addWidget(self.progress_bar)
            self.export_btn = QPushButton("Export My Results")
            self.export_btn.clicked.connect(self.export_my_results)
            top.addWidget(self.export_btn)
            logout_btn = QPushButton("Logout")
            logout_btn.clicked.connect(lambda: self._escape_after_dump(on_logout))
            top.addWidget(logout_btn)
            root.addLayout(top)

            splitter = QSplitter()
            root.addWidget(splitter)

            self.sample_list = QListWidget()
            self.sample_list.itemSelectionChanged.connect(self.select_sample)
            self.sample_list.setMaximumWidth(70)
            self.sample_list.setMinimumWidth(50)
            splitter.addWidget(self.sample_list)

            self.reader = QScrollArea()
            self.reader.setWidgetResizable(True)
            reader_body = QWidget()
            reader_layout = QVBoxLayout(reader_body)
            self.sample_title = QLabel()
            self.sample_title.setStyleSheet("font-size: 18px; font-weight: 700;")
            self.note = QTextEdit()
            self.note.setReadOnly(True)
            self.note.setMinimumHeight(NOTE_MIN_HEIGHT)
            self.note.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            reader_layout.addWidget(self.sample_title)
            reader_layout.addWidget(QLabel("Discharge Note"))
            reader_layout.addWidget(self.note, 1)
            self.reader.setWidget(reader_body)
            splitter.addWidget(self.reader)

            course_panel = QWidget()
            course_layout = QVBoxLayout(course_panel)
            # Empty stand-in for the reader column's sample title, so the two
            # column headers line up.
            course_spacer = QLabel()
            course_spacer.setStyleSheet("font-size: 18px; font-weight: 700;")
            self.course = QTextEdit()
            self.course.setReadOnly(True)
            self.course.setMinimumHeight(COURSE_MIN_HEIGHT)
            self.course.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            course_layout.addWidget(course_spacer)
            course_layout.addWidget(QLabel("Brief Hospital Course"))
            course_layout.addWidget(self.course, 1)
            splitter.addWidget(course_panel)

            score_panel = QWidget()
            score_panel.setMaximumWidth(420)
            score_panel.setMinimumWidth(360)
            score_layout = QVBoxLayout(score_panel)
            score_layout.setContentsMargins(8, 4, 8, 4)
            score_layout.setSpacing(6)
            self.timer_display = QLabel("00:00")
            self.timer_display.setStyleSheet("font-size: 32px; font-weight: 700;")
            score_layout.addWidget(self.timer_display)

            timer_buttons = QGridLayout()
            self.start_pause_btn = QPushButton("Start")
            self.reset_btn = QPushButton("Reset")
            self.end_btn = QPushButton("End")
            self.previous_btn = QPushButton(SAMPLE_NAV_LABELS["previous"])
            self.next_btn = QPushButton(SAMPLE_NAV_LABELS["next"])
            self.previous_btn.setToolTip("Previous sample")
            self.next_btn.setToolTip("Next sample")
            self.start_pause_btn.clicked.connect(self.toggle_start_pause)
            self.reset_btn.clicked.connect(self.reset)
            self.end_btn.clicked.connect(self.end)
            self.previous_btn.clicked.connect(self.previous_sample)
            self.next_btn.clicked.connect(self.next_sample)
            timer_buttons.addWidget(self.start_pause_btn, 0, 0)
            timer_buttons.addWidget(self.end_btn, 0, 1)
            timer_buttons.addWidget(self.reset_btn, 0, 2)
            timer_buttons.addWidget(self.previous_btn, 1, 0)
            timer_buttons.addWidget(self.next_btn, 1, 1)
            score_layout.addLayout(timer_buttons)
            self.notice = QLabel(NOTICE_DEFAULT)
            score_layout.addWidget(self.notice)

            self.metric_checks = {}
            self.correctness_counts = {}
            self.score_labels = {}
            for metric, labels in EVALUATION_DEDUCTION_OPTIONS.items():
                group = QGroupBox(metric.title())
                group_layout = QVBoxLayout(group)
                self.score_labels[metric] = QLabel("Score: 5")
                group_layout.addWidget(self.score_labels[metric])
                if metric == "correctness":
                    for key in CORRECTNESS_DEDUCTION_KEYS:
                        counter_row = QHBoxLayout()
                        label = QLabel(CORRECTNESS_COUNT_LABELS[key])
                        spin = QSpinBox()
                        spin.setRange(0, CORRECTNESS_COUNT_MAX)
                        spin.setSingleStep(1)
                        spin.valueChanged.connect(self.update_scores)
                        counter_row.addWidget(label, 1)
                        counter_row.addWidget(spin)
                        group_layout.addLayout(counter_row)
                        self.correctness_counts[key] = spin
                    score_layout.addWidget(group)
                    continue
                checks = []
                for key, text in labels:
                    check = QCheckBox(text)
                    check.stateChanged.connect(self.update_scores)
                    group_layout.addWidget(check)
                    checks.append((key, check))
                self.metric_checks[metric] = checks
                score_layout.addWidget(group)

            self.overall = QComboBox()
            self.overall.addItem("Select", "")
            for score, text in OVERALL_QUALITY_OPTIONS:
                self.overall.addItem(f"{score} · {text}", score)
            score_layout.addWidget(QLabel("Overall Quality"))
            score_layout.addWidget(self.overall)

            self.comment = QTextEdit()
            self.comment.setPlaceholderText("전체 평가에 대한 자유서술 1개")
            score_layout.addWidget(QLabel("Detailed comment · optional"))
            score_layout.addWidget(self.comment)
            splitter.addWidget(score_panel)
            splitter.setSizes([60, 480, 480, 380])
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setStretchFactor(2, 1)
            splitter.setStretchFactor(3, 0)

            self.refresh_list()
            self.lock_scoring(True)
            self.refresh_sample()
            self._maybe_prompt_recovery()

        def refresh_list(self):
            self.sample_list.blockSignals(True)
            self.sample_list.clear()
            for sample in self.samples:
                label = f"{sample['display_order']}"
                if sample["completed"]:
                    label += " ✓"
                if sample["sample_id"] in self._recovery_dumps:
                    label += " ⟳"
                self.sample_list.addItem(QListWidgetItem(label))
            if self.samples:
                self.sample_list.setCurrentRow(self.current_index)
            self.sample_list.blockSignals(False)
            completed = sum(1 for sample in self.samples if sample["completed"])
            total = len(self.samples)
            self.progress_label.setText(f"{completed} / {total} 완료")
            self.progress_bar.setValue(_progress_percent(completed, total))

        def select_sample(self):
            row = self.sample_list.currentRow()
            if row < 0 or row == self.current_index:
                return
            previous = self.current_index
            if self.started_at is not None:
                self.sample_list.blockSignals(True)
                self.sample_list.setCurrentRow(previous)
                self.sample_list.blockSignals(False)
                if not self._confirm_unfinished():
                    return
            self.current_index = min(row, len(self.samples) - 1)
            self.sample_list.blockSignals(True)
            self.sample_list.setCurrentRow(self.current_index)
            self.sample_list.blockSignals(False)
            self.refresh_sample()

        def refresh_sample(self):
            if not self.samples:
                return
            sample = self.samples[self.current_index]
            self.sample_title.setText(f"Sample {sample['sample_id']} · order {sample['display_order']}")
            self.note.setPlainText(sample["discharge_note"])
            self.course.setPlainText(sample["brief_hospital_course"])
            self.reset_timer_state()
            self.lock_scoring(True)
            self.clear_scoring()
            if sample["completed"]:
                self._load_saved_evaluation(sample["sample_id"])
            if sample["sample_id"] in self._recovery_dumps:
                self._apply_recovery_dump(self._recovery_dumps[sample["sample_id"]])

        def export_my_results(self):
            target, _ = QFileDialog.getSaveFileName(
                self,
                "Export My Results",
                str(
                    store.db_path.parent.parent
                    / "exports"
                    / f"{_safe_filename_part(self.user.user_id)}_results.jsonl"
                ),
                "JSONL (*.jsonl)",
            )
            if not target:
                return
            rows = store.export_rows(self.user.user_id)
            try:
                write_jsonl(rows, target)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Export failed",
                    f"Could not write your results.\n\n{exc}\n\nNo file was saved at:\n{target}",
                )
                return
            QMessageBox.information(
                self,
                "Exported",
                f"Saved {len(rows)} evaluation(s) to:\n{target}",
            )

        def _load_saved_evaluation(self, sample_id):
            evaluation = store.get_evaluation(self.user.user_id, sample_id)
            if not evaluation:
                return
            deductions = json.loads(evaluation["deductions_json"])
            correctness = deductions.get("correctness", {}) or {}
            for metric in ("completeness", "readability"):
                selected = deductions.get(metric, []) or []
                for key, check in self.metric_checks[metric]:
                    check.setChecked(key in selected)
            for key, spin in self.correctness_counts.items():
                spin.setValue(int(correctness.get(key, 0)))
            overall = evaluation["scores"].get("overall_quality")
            index = self.overall.findData(overall) if overall is not None else 0
            self.overall.setCurrentIndex(index if index >= 0 else 0)
            self.comment.setPlainText(evaluation["detailed_comment"] or "")
            self.update_scores()
            self.elapsed_seconds = evaluation.get("active_seconds") or 0
            self.update_timer_display()
            self.paused_seconds = evaluation.get("paused_seconds") or 0
            self.pause_count = evaluation.get("pause_count") or 0
            self.reset_count = evaluation.get("reset_count") or 0
            self.llm_open_count = evaluation.get("llm_judge_open_count") or 0
            self.llm_visible_seconds = evaluation.get("llm_judge_visible_seconds") or 0
            first_opened = evaluation.get("llm_judge_first_opened_at")
            self.llm_first_opened_at = (
                datetime.fromisoformat(first_opened) if first_opened else None
            )
            self.start_pause_btn.setText("Edit")
            self.notice.setText(NOTICE_EDIT)

        def _quarantine_dump(self, path):
            """Move an unreadable dump aside so it is not rescanned every login.

            Returns 1 if a file was set aside (so the caller can count it), 0
            otherwise. Never raises.
            """
            try:
                path.rename(path.with_suffix(path.suffix + ".corrupt"))
                return 1
            except OSError:
                return 0

        def _scan_recovery_dumps(self):
            """Find this evaluator's stranded ``unsaved-*.json`` dumps.

            Returns ``(dumps, files, skipped, corrupt)`` where ``dumps`` maps
            sample_id to the most recent dump payload that can still be
            re-entered into the form, ``files`` maps sample_id to every dump file
            for that sample (deleted once the recovered work is saved),
            ``skipped`` counts dumps that cannot be auto-recovered (unknown
            sample, or one the evaluator has since completed), and ``corrupt``
            counts dumps that were unreadable/truncated and set aside.
            """
            target_dir = store.db_path.parent
            known_ids = {sample["sample_id"] for sample in self.samples}
            completed_ids = {
                sample["sample_id"] for sample in self.samples if sample["completed"]
            }
            latest = {}
            files = {}
            skipped = 0
            corrupt = 0
            for path in sorted(target_dir.glob("unsaved-*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or not data.get("sample_id"):
                        raise ValueError("dump has no usable sample_id")
                except (OSError, ValueError):
                    # Truncated or malformed (e.g. power loss mid-write). Count
                    # and quarantine it instead of silently skipping forever, so
                    # the evaluator is at least told work could not be recovered.
                    corrupt += self._quarantine_dump(path)
                    continue
                if data.get("evaluator_id") != self.user.user_id:
                    continue
                sample_id = data.get("sample_id")
                if sample_id not in known_ids or sample_id in completed_ids:
                    skipped += 1
                    continue
                files.setdefault(sample_id, []).append(path)
                dumped_at = data.get("dumped_at") or ""
                if sample_id not in latest or dumped_at >= latest[sample_id][0]:
                    latest[sample_id] = (dumped_at, data)
            dumps = {sample_id: payload for sample_id, (_, payload) in latest.items()}
            return dumps, files, skipped, corrupt

        def _maybe_prompt_recovery(self):
            # Runs during login (EvaluatorView construction). A failure here must
            # never bubble out to the Qt slot that built this view: that leaves
            # the evaluator stranded on the login screen with no message. Contain
            # everything and, worst case, just open the workbench normally.
            try:
                dumps, files, skipped, corrupt = self._scan_recovery_dumps()
            except Exception:
                return

            stranded = skipped + corrupt
            if not dumps:
                # Nothing auto-recoverable, but there may still be stranded work
                # (dumps for already-completed samples, or corrupt files).
                # Surface it rather than returning silently.
                if stranded:
                    QMessageBox.information(
                        self,
                        "복구할 수 없는 백업",
                        f"저장되지 않은 평가 백업 {stranded}건이 있으나 자동으로 "
                        "복구할 수 없었습니다.\n"
                        "(현재 샘플 목록과 맞지 않거나, 이미 완료했거나, 파일이 "
                        "손상되었습니다.)\n\n"
                        "필요하면 storage 폴더의 unsaved-*.json 파일을 관리자에게 "
                        "전달하세요.",
                    )
                return

            count = len(dumps)
            extra = (
                f"\n\n(다른 {stranded}건은 현재 샘플 목록과 맞지 않거나 이미 완료됐거나 "
                "파일이 손상되어 자동 복구하지 않았습니다.)"
                if stranded
                else ""
            )
            answer = QMessageBox.question(
                self,
                "저장되지 않은 평가 복구",
                f"이전에 저장하지 못한 평가 {count}건을 발견했습니다.\n"
                "화면으로 불러와 마저 작성하고 저장할 수 있습니다.\n\n"
                "복구하시겠습니까?" + extra,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                return
            self._recovery_dumps = dumps
            self._recovery_files = files
            order = {
                sample["sample_id"]: index
                for index, sample in enumerate(self.samples)
            }
            first_index = min(order[sample_id] for sample_id in dumps)
            self.current_index = first_index
            self.refresh_list()
            self.refresh_sample()
            QMessageBox.information(
                self,
                "복구 완료",
                f"저장되지 않았던 평가 {count}건을 불러왔습니다.\n"
                "내용을 확인하고 Overall Quality를 선택한 뒤 End로 저장하세요.\n"
                "목록의 ⟳ 표시가 복구된 샘플입니다.",
            )

        def _apply_recovery_dump(self, dump):
            # Every value here comes from a JSON file on disk that may be
            # malformed. Coerce defensively -- a bad numeric string must degrade
            # to 0, never raise, because this runs on the login path and a
            # single malformed dump must not block login. The dump is
            # re-opened editable and paused, so any coerced field is visible for
            # the evaluator to correct before saving.
            if not isinstance(dump, dict):
                return
            deductions = dump.get("deductions") or {}
            correctness = deductions.get("correctness") or {}
            if not isinstance(correctness, dict):
                correctness = {}
            for metric in ("completeness", "readability"):
                selected = deductions.get(metric) or []
                if not isinstance(selected, (list, tuple, set)):
                    selected = []
                for key, check in self.metric_checks[metric]:
                    check.setChecked(key in selected)
            for key, spin in self.correctness_counts.items():
                spin.setValue(max(0, _coerce_int(correctness.get(key, 0))))
            scores = dump.get("scores") or {}
            if not isinstance(scores, dict):
                scores = {}
            overall = scores.get("overall_quality")
            index = self.overall.findData(overall) if overall is not None else 0
            self.overall.setCurrentIndex(index if index >= 0 else 0)
            comment = dump.get("detailed_comment")
            self.comment.setPlainText(comment if isinstance(comment, str) else "")
            self.update_scores()
            # Restore the original effort metrics so the re-saved record keeps
            # the timer/LLM-view telemetry the evaluator already accrued.
            timer = dump.get("timer") or {}
            if not isinstance(timer, dict):
                timer = {}
            self.elapsed_seconds = max(0, _coerce_int(timer.get("active_seconds")))
            self.paused_seconds = max(0, _coerce_int(timer.get("paused_seconds")))
            self.pause_count = max(0, _coerce_int(timer.get("pause_count")))
            self.reset_count = max(0, _coerce_int(timer.get("reset_count")))
            judge = dump.get("llm_judge_view") or {}
            if not isinstance(judge, dict):
                judge = {}
            self.llm_open_count = max(0, _coerce_int(judge.get("open_count")))
            self.llm_visible_seconds = max(0, _coerce_int(judge.get("visible_seconds")))
            # Re-open in a paused, editable state: the evaluator reviews the
            # recovered inputs and presses Resume to continue timing from the
            # restored elapsed time. The timer stays stopped until then.
            self.started_at = now()
            self.active_started_at = None
            self.lock_scoring(False)
            self.start_pause_btn.setText("Resume")
            self.update_timer_display()
            self._start_autosave()

        def _discard_recovery_for_sample(self, sample_id, keep_backup=False):
            """Drop pending recovery dumps for a sample.

            After a successful save the dumps are redundant, so they are deleted.
            When the evaluator instead *resets* (throwing the recovered work
            away), keep a ``.discarded`` copy rather than unlinking: a Reset used
            to be the one irreversible way to destroy the only copy of unsaved
            work with no snapshot behind it.
            """
            self._recovery_dumps.pop(sample_id, None)
            for path in self._recovery_files.pop(sample_id, []):
                try:
                    if keep_backup:
                        path.rename(path.with_suffix(path.suffix + ".discarded"))
                    else:
                        path.unlink()
                except OSError:
                    pass

        def lock_scoring(self, locked):
            for checks in self.metric_checks.values():
                for _, check in checks:
                    check.setEnabled(not locked)
            for spin in self.correctness_counts.values():
                spin.setEnabled(not locked)
            self.overall.setEnabled(not locked)
            self.comment.setEnabled(not locked)
            self.end_btn.setEnabled(not locked)
            self.notice.setVisible(locked)

        def clear_scoring(self):
            for checks in self.metric_checks.values():
                for _, check in checks:
                    check.setChecked(False)
            for spin in self.correctness_counts.values():
                spin.setValue(0)
            self.overall.setCurrentIndex(0)
            self.comment.clear()
            self.update_scores()

        def _current_sample_completed(self):
            return bool(self.samples and self.samples[self.current_index].get("completed"))

        def _confirm_reset_current_evaluation(self):
            has_recovery = (
                bool(self.samples)
                and self.samples[self.current_index]["sample_id"]
                in self._recovery_dumps
            )
            recovery_line = (
                "복구된(아직 저장되지 않은) 입력도 함께 사라집니다. "
                "storage 폴더에 .discarded 백업이 남습니다.\n\n"
                if has_recovery
                else ""
            )
            body = (
                "현재 화면의 점수·자유서술과 타이머가 모두 초기화되어 평가 전 상태로 돌아갑니다.\n\n"
                "이미 저장된 평가가 있으면 함께 삭제되고, 목록의 완료 표시(✓)도 사라집니다.\n\n"
                + recovery_line
                + "계속하시겠습니까?"
            )
            answer = QMessageBox.question(
                self,
                "평가 초기화",
                body,
                QMessageBox.Ok | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            return answer == QMessageBox.Ok

        def toggle_start_pause(self):
            if self.started_at is None:
                self.start()
            elif self.timer.isActive():
                self.pause()
            else:
                self.resume()

        def start(self):
            current = now()
            self.started_at = current
            self.active_started_at = current
            if not self._current_sample_completed():
                self.elapsed_seconds = 0
            self.timer.start()
            self.lock_scoring(False)
            self.start_pause_btn.setText("Pause")
            self._start_autosave()

        def pause(self):
            self.sync_timer_state()
            self.timer.stop()
            self.pause_count += 1
            self.pause_started_at = now()
            self.active_started_at = None
            self.start_pause_btn.setText("Resume")

        def resume(self):
            self.record_paused_seconds()
            self.active_started_at = now()
            self.timer.start()
            self.start_pause_btn.setText("Pause")
            if not self.autosave_timer.isActive():
                self._start_autosave()

        def reset(self):
            if not self._confirm_reset_current_evaluation():
                return
            # Delete any saved record first so the reset truly persists: a
            # revisit shows a blank form and the list ✓ disappears. Abort the
            # whole reset if the delete fails, so the cleared screen never
            # disagrees with what is still stored.
            if not self._discard_saved_evaluation_for_current_sample():
                return
            self._stop_autosave(delete_draft=True)
            self.reset_count += 1
            self.reset_timer_state(preserve_reset_count=True)
            self.lock_scoring(True)
            self.clear_scoring()
            self.start_pause_btn.setEnabled(True)

        def _discard_saved_evaluation_for_current_sample(self):
            """Return the current sample to the un-evaluated state.

            Deletes any persisted evaluation AND drops any pending recovery
            dump for this sample, so a Reset always persists: a revisit shows a
            blank form with no ✓ (completed) nor ⟳ (recovery) marker. Returns
            True when nothing needed removing or removal succeeded; False only
            when a saved record could not be deleted.
            """
            if not self.samples:
                return True
            sample_id = self.samples[self.current_index]["sample_id"]
            was_completed = self._current_sample_completed()
            had_recovery = sample_id in self._recovery_dumps
            if not was_completed and not had_recovery:
                return True
            if was_completed:
                try:
                    store.delete_evaluation(self.user.user_id, sample_id)
                except Exception as exc:
                    QMessageBox.critical(
                        self,
                        "초기화 실패",
                        "저장된 평가를 삭제하지 못해 초기화를 취소했습니다.\n\n"
                        f"{exc}",
                    )
                    return False
                self.samples = store.list_samples_for_evaluator(self.user.user_id)
            if had_recovery:
                # Abandon the recovered (previously unsaved) inputs too, so the
                # reset is not silently re-applied from the dump on revisit --
                # but keep a .discarded backup so the reset is not the one
                # irreversible way to destroy unsaved work.
                self._discard_recovery_for_sample(sample_id, keep_backup=True)
            # Reflect removals in the list: ✓/⟳ markers and the progress counter.
            self.refresh_list()
            return True

        def end(self):
            if not self._save_current_evaluation():
                return
            QMessageBox.information(self, "Saved", "Evaluation saved.")
            self.samples = store.list_samples_for_evaluator(self.user.user_id)
            self.refresh_list()
            self.next_sample()

        def _evaluation_payload(self):
            self.sync_timer_state()
            self.timer.stop()
            self.record_paused_seconds()
            self.flush_llm_judge_visibility()
            sample = self.samples[self.current_index]
            timer = {
                "started_at": _iso(self.started_at),
                "ended_at": _iso(now()),
                "active_seconds": self.elapsed_seconds,
                "paused_seconds": self.paused_seconds,
                "pause_count": self.pause_count,
                "reset_count": self.reset_count,
            }
            llm_view = {
                "opened": self.llm_open_count > 0,
                "first_opened_at": _iso(self.llm_first_opened_at),
                "open_count": self.llm_open_count,
                "visible_seconds": self.llm_visible_seconds,
            }
            return {
                "evaluator_id": self.user.user_id,
                "sample_id": sample["sample_id"],
                "display_order": sample["display_order"],
                "scores": self.current_scores(),
                "deductions": self.current_deductions(),
                "detailed_comment": self.comment.toPlainText(),
                "timer": timer,
                "llm_judge_view": llm_view,
            }

        def _persist_with_verify(self, payload):
            for attempt in range(2):
                try:
                    return store.save_and_verify_evaluation(**payload)
                except Exception as exc:
                    self._last_save_error = exc
                    if attempt == 0:
                        continue
                    raise

        def _draft_path_for_current_sample(self):
            sample = self.samples[self.current_index]
            evaluator_id = _safe_filename_part(self.user.user_id)
            sample_ref = _safe_filename_part(pseudonymize_id(sample["sample_id"]))
            # Stable name (no timestamp) so successive autosaves overwrite one
            # file instead of littering the folder with hundreds of drafts.
            return store.db_path.parent / f"unsaved-{evaluator_id}-{sample_ref}-draft.json"

        def _draft_payload(self):
            """A read-only snapshot of the in-progress form for autosave.

            Unlike ``_evaluation_payload`` this does NOT stop the timer or flush
            the LLM-visibility clock -- autosave must not perturb the live
            session it is protecting.
            """
            self.sync_timer_state()
            sample = self.samples[self.current_index]
            return {
                "evaluator_id": self.user.user_id,
                "sample_id": sample["sample_id"],
                "display_order": sample["display_order"],
                "scores": self.current_scores(),
                "deductions": self.current_deductions(),
                "detailed_comment": self.comment.toPlainText(),
                "timer": {
                    "started_at": _iso(self.started_at),
                    "ended_at": None,
                    "active_seconds": self.elapsed_seconds,
                    "paused_seconds": self.paused_seconds,
                    "pause_count": self.pause_count,
                    "reset_count": self.reset_count,
                },
                "llm_judge_view": {
                    "opened": self.llm_open_count > 0,
                    "first_opened_at": _iso(self.llm_first_opened_at),
                    "open_count": self.llm_open_count,
                    "visible_seconds": self.llm_visible_seconds,
                },
            }

        def _start_autosave(self):
            if not self.samples:
                return
            self._draft_path = self._draft_path_for_current_sample()
            self._autosave_draft()  # write an immediate draft, don't wait 10s
            self.autosave_timer.start()

        def _stop_autosave(self, delete_draft):
            self.autosave_timer.stop()
            if delete_draft and self._draft_path is not None:
                try:
                    self._draft_path.unlink()
                except OSError:
                    pass
            self._draft_path = None

        def _autosave_draft(self):
            # Only while an evaluation is genuinely in progress and unsaved.
            if self.started_at is None or self._draft_path is None or not self.samples:
                return
            try:
                payload = self._draft_payload()
                payload["dumped_at"] = now().isoformat()
                payload["error"] = "autosave-draft (in progress, not yet saved)"
                _atomic_write_text(
                    self._draft_path,
                    json.dumps(payload, ensure_ascii=False, indent=2),
                )
                restrict_file(self._draft_path)
            except Exception:
                # Autosave is best-effort insurance; never interrupt the session.
                pass

        def _record_unsaved_dump(self, payload, exc):
            target_dir = store.db_path.parent
            dumped_at = now().isoformat()
            stamp = now().strftime("%Y%m%d-%H%M%S-%f")
            evaluator_id = _safe_filename_part(payload["evaluator_id"])
            # sample_id may be patient-linked. The dump's *contents* are owner-only, but a
            # filename travels through directory listings, backup indexes and
            # screen shares, so keep the identifier out of it.
            sample_ref = _safe_filename_part(pseudonymize_id(payload["sample_id"]))
            dump_path = target_dir / f"unsaved-{evaluator_id}-{sample_ref}-{stamp}.json"
            dump = dict(payload)
            dump["dumped_at"] = dumped_at
            dump["error"] = repr(exc)
            serialized = json.dumps(dump, ensure_ascii=False, indent=2)
            try:
                # Atomic: a crash mid-write must not leave a half-written dump
                # that the recovery scanner then treats as corrupt.
                _atomic_write_text(dump_path, serialized)
                # The dump carries the full sample_id and the clinician's free
                # text -- the most sensitive payload the app writes. Default
                # umask would leave it 0644 next to a 0600 database.
                restrict_file(dump_path)
                log_path = target_dir / "save_failures.log"
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        f"{dumped_at} evaluator={payload['evaluator_id']} "
                        f"sample={sample_ref} dump={dump_path.name} error={repr(exc)}\n"
                    )
                restrict_file(log_path)
            except Exception as dump_exc:
                # The dump itself failed (read-only dir / disk full — often the
                # same root cause as the original save failure). This is the
                # last line of defense, so it must never raise: fall back to the
                # clipboard + an on-screen copy the user can manually save.
                self._last_unsaved_dump_path = None
                # This is the last line of defense and is called from inside an
                # exception handler, so it must not raise on its way out.
                try:
                    self._present_unsaved_fallback(serialized, dump_exc)
                except Exception:
                    pass
                return None
            self._last_unsaved_dump_path = dump_path
            return dump_path

        def _present_unsaved_fallback(self, serialized, dump_exc):
            # The payload carries the clinician's free-text comment, which may
            # quote the note. Copying it to the system clipboard puts it beyond
            # the app's reach -- clipboard managers, Universal Clipboard, RDP
            # redirection -- and it outlives the process. Still offer the copy,
            # since the alternative here is losing the evaluation, but make it a
            # deliberate press rather than something that happens silently.
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("저장/백업 실패")
            box.setText(
                "평가를 저장하지 못했고 백업 파일도 기록하지 못했습니다.\n"
                "(저장 위치가 읽기 전용이거나 디스크가 가득 찼을 수 있습니다.)\n\n"
                "아래 'Show Details'에서 내용을 확인할 수 있습니다. "
                "'클립보드로 복사'를 누르면 복사되며, 안전한 곳에 붙여넣어 보관한 뒤 "
                "관리자에게 전달하세요."
            )
            box.setDetailedText(serialized)
            copy_btn = box.addButton("클립보드로 복사", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            # QMessageBox wires every added button to close the dialog. Detach
            # that so copying does not dismiss the only copy of the evaluation.
            # Whether a connection exists is a Qt implementation detail, so a
            # bare disconnect() is not safe to assume.
            try:
                copy_btn.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            copy_btn.clicked.connect(lambda: self._copy_to_clipboard(serialized))
            box.exec()

        def _copy_to_clipboard(self, serialized):
            try:
                clipboard = QApplication.clipboard()
                if clipboard is not None:
                    clipboard.setText(serialized)
            except Exception:
                pass

        def _save_current_evaluation(self):
            if self.overall.currentData() in (None, ""):
                QMessageBox.warning(
                    self,
                    "Overall Quality 필요",
                    "Overall Quality를 선택해야 평가를 저장할 수 있습니다.",
                )
                return False
            was_timer_active = self.timer.isActive()
            payload = self._evaluation_payload()
            try:
                self._persist_with_verify(payload)
            except Exception as exc:
                dump_path = self._record_unsaved_dump(payload, exc)
                if was_timer_active and self.started_at is not None:
                    self.active_started_at = now()
                    self.timer.start()
                    self.start_pause_btn.setText("Pause")
                if dump_path is not None:
                    QMessageBox.critical(
                        self,
                        "Save failed",
                        (
                            "Evaluation could not be verified after retry.\n\n"
                            "Your inputs were preserved and dumped. You may log out without losing this dumped work.\n"
                            f"Dump path:\n{dump_path}"
                        ),
                    )
                # When dump_path is None the dump also failed; the user was
                # already shown the clipboard-fallback dialog by
                # _record_unsaved_dump, so do not show a misleading dump path.
                return False
            self._last_unsaved_dump_path = None
            self._last_save_error = None
            self._discard_recovery_for_sample(payload["sample_id"])
            self._stop_autosave(delete_draft=True)
            self.started_at = None
            self.active_started_at = None
            return True

        def _escape_after_dump(self, callback):
            if self.started_at is None:
                callback()
                return
            if self._last_unsaved_dump_path is not None:
                payload = self._evaluation_payload()
                new_path = self._record_unsaved_dump(
                    payload,
                    self._last_save_error or RuntimeError("logout after failed save"),
                )
                # Only leave if the work was actually re-dumped. If the dump
                # failed too, the clipboard fallback was shown; keep the user
                # here so they don't lose work to a clipboard-only copy.
                if new_path is not None:
                    callback()
                return
            if self.overall.currentData() in (None, ""):
                # Cannot save normally (Overall Quality unset). Do NOT silently
                # discard the in-progress evaluation: ask the user, and only
                # leave after dumping their inputs to a backup file.
                answer = QMessageBox.question(
                    self,
                    "저장되지 않은 평가",
                    "현재 샘플에 저장되지 않은 평가가 있습니다.\n"
                    "Overall Quality가 선택되지 않아 정상 저장할 수 없습니다.\n\n"
                    "예: 입력 내용을 백업 파일로 보관하고 로그아웃\n"
                    "아니오: 평가 화면으로 돌아가기",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
                payload = self._evaluation_payload()
                dump_path = self._record_unsaved_dump(
                    payload, RuntimeError("logout with Overall Quality unset")
                )
                # Only leave if the inputs were actually preserved to a file. If
                # the dump failed, the clipboard fallback was shown; keep the
                # user here so they don't lose that clipboard-only copy.
                if dump_path is not None:
                    callback()
                return
            if self._save_current_evaluation():
                callback()
                return
            # Save failed: _save_current_evaluation already wrote a dump (or
            # showed the clipboard fallback). Only leave if work was preserved.
            if self._last_unsaved_dump_path is not None:
                callback()

        def confirm_app_close(self):
            """Return True if the window may close without losing work."""
            if self.started_at is None:
                return True
            if self._last_unsaved_dump_path is not None:
                payload = self._evaluation_payload()
                new_path = self._record_unsaved_dump(
                    payload,
                    self._last_save_error or RuntimeError("window close after failed save"),
                )
                # Allow close only if the re-dump succeeded; otherwise the
                # clipboard fallback was shown and we keep the window open.
                return new_path is not None
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("저장되지 않은 평가")
            box.setText("현재 샘플에 저장되지 않은 평가가 있습니다.")
            box.setInformativeText(
                "저장 후 종료하거나, 입력 내용을 백업 파일로 보관한 뒤 종료할 수 있습니다."
            )
            save_btn = box.addButton("저장 후 종료", QMessageBox.AcceptRole)
            dump_btn = box.addButton("백업 후 종료", QMessageBox.DestructiveRole)
            cancel_btn = box.addButton("취소", QMessageBox.RejectRole)
            box.setDefaultButton(cancel_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn:
                return False
            if clicked is save_btn:
                if self._save_current_evaluation():
                    return True
                # Validation/persistence failure: only allow close if the work
                # was preserved to a dump file.
                return self._last_unsaved_dump_path is not None
            payload = self._evaluation_payload()
            dump_path = self._record_unsaved_dump(
                payload, RuntimeError("window close, user chose backup")
            )
            # Allow close only if the backup was actually written; otherwise the
            # clipboard fallback was shown and we keep the window open.
            return dump_path is not None

        def _confirm_unfinished(self):
            if self.started_at is None:
                return True
            answer = QMessageBox.question(
                self,
                "확인",
                "작업을 끝마치겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return False
            if not self._save_current_evaluation():
                return False
            self.samples = store.list_samples_for_evaluator(self.user.user_id)
            self.refresh_list()
            return True

        def previous_sample(self):
            if not self.samples:
                return
            if not self._confirm_unfinished():
                return
            self.current_index = max(self.current_index - 1, 0)
            self.sample_list.setCurrentRow(self.current_index)
            self.refresh_sample()

        def next_sample(self):
            if not self.samples:
                return
            if not self._confirm_unfinished():
                return
            self.current_index = min(self.current_index + 1, len(self.samples) - 1)
            self.sample_list.setCurrentRow(self.current_index)
            self.refresh_sample()

        def tick(self):
            self.sync_timer_state()

        def sync_timer_state(self):
            if self.active_started_at is not None:
                current = now()
                whole_seconds = int((current - self.active_started_at).total_seconds())
                if whole_seconds > 0:
                    self.elapsed_seconds += whole_seconds
                    self.active_started_at += timedelta(seconds=whole_seconds)
            self.update_timer_display()

        def update_timer_display(self):
            minutes, seconds = divmod(self.elapsed_seconds, 60)
            self.timer_display.setText(f"{minutes:02d}:{seconds:02d}")

        def record_paused_seconds(self):
            if self.pause_started_at is None:
                return
            paused_seconds = int((now() - self.pause_started_at).total_seconds())
            self.paused_seconds += max(0, paused_seconds)
            self.pause_started_at = None

        def flush_llm_judge_visibility(self):
            # The evaluator screen no longer shows an LLM-as-Judge panel, so
            # llm_last_opened stays None and this records nothing. Kept so the
            # saved llm_judge_view payload (and its columns) keep their shape.
            if self.llm_last_opened is None:
                return
            visible_seconds = int((now() - self.llm_last_opened).total_seconds())
            self.llm_visible_seconds += max(0, visible_seconds)
            self.llm_last_opened = None

        def reset_timer_state(self, preserve_reset_count=False):
            reset_count = self.reset_count if preserve_reset_count else 0
            self.timer.stop()
            # No evaluation is in progress after this; stop autosaving. The draft
            # (if any) is left on disk: it is either already gone (saved) or is
            # genuine unsaved work that recovery should still find.
            self._stop_autosave(delete_draft=False)
            self.started_at = None
            self.active_started_at = None
            self.elapsed_seconds = 0
            self.paused_seconds = 0
            self.pause_started_at = None
            self.pause_count = 0
            self.reset_count = reset_count
            self.llm_open_count = 0
            self.llm_first_opened_at = None
            self.llm_visible_seconds = 0
            self.llm_last_opened = None
            self.notice.setText(NOTICE_DEFAULT)
            self.timer_display.setText("00:00")
            self.start_pause_btn.setText("Start")
            self.start_pause_btn.setEnabled(True)

        def update_scores(self):
            deductions = self.current_deductions()
            for metric in ("completeness", "correctness", "readability"):
                score = score_from_deductions(metric, deductions[metric])
                if metric == "correctness":
                    error_count = sum(deductions["correctness"].values())
                    self.score_labels[metric].setText(
                        f"Score: {score} · Errors: {error_count} · max deduction -4"
                    )
                    continue
                self.score_labels[metric].setText(f"Score: {score}")

        def current_deductions(self):
            deductions = {
                metric: [key for key, check in checks if check.isChecked()]
                for metric, checks in self.metric_checks.items()
            }
            deductions["correctness"] = {
                key: spin.value() for key, spin in self.correctness_counts.items()
            }
            return deductions

        def current_scores(self):
            deductions = self.current_deductions()
            return {
                "completeness": score_from_deductions("completeness", deductions["completeness"]),
                "correctness": score_from_deductions("correctness", deductions["correctness"]),
                "readability": score_from_deductions("readability", deductions["readability"]),
                "overall_quality": self.overall.currentData() or None,
            }

    class AdminView(QWidget):
        def __init__(self, on_logout, on_review):
            super().__init__()
            self.on_review = on_review
            layout = QVBoxLayout(self)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(GAP)
            top = QHBoxLayout()
            title = QLabel("Admin Dashboard")
            title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {TEXT};")
            top.addWidget(title)
            top.addStretch()
            logout_btn = QPushButton("Logout")
            logout_btn.setMinimumHeight(36)
            logout_btn.setStyleSheet(button_qss("secondary"))
            logout_btn.clicked.connect(on_logout)
            top.addWidget(logout_btn)
            layout.addLayout(top)

            hero = apply_card(QFrame())
            hero_layout = QVBoxLayout(hero)
            hero_layout.setContentsMargins(18, 16, 18, 16)
            hero_layout.setSpacing(10)
            hero_top = QHBoxLayout()
            hero_text = QVBoxLayout()
            hero_text.setSpacing(6)
            self.summary = QLabel()
            self.hero_number = QLabel()
            self.hero_number.setStyleSheet(f"color: {TEXT}; font-size: 32px; font-weight: 800;")
            self.summary.setStyleSheet(f"color: {TEXT}; font-size: 14px; font-weight: 700;")
            self.evaluator_summary = QLabel()
            self.evaluator_summary.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
            hero_text.addWidget(self.hero_number)
            hero_text.addWidget(self.summary)
            hero_top.addLayout(hero_text, 1)

            export_actions = QVBoxLayout()
            export_actions.setSpacing(8)
            actions_label = QLabel("Data actions")
            actions_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; font-weight: 700;")
            action_row = QHBoxLayout()
            action_row.setSpacing(8)
            review_btn = QPushButton("Review")
            csv_btn = QPushButton("Export CSV")
            jsonl_btn = QPushButton("Export JSONL")
            import_btn = QPushButton("Import Results")
            reset_btn = QPushButton("Reset")
            reset_all_btn = QPushButton("Full Reset")
            for button in (review_btn, csv_btn, jsonl_btn, import_btn, reset_btn, reset_all_btn):
                button.setMinimumHeight(36)
            review_btn.setStyleSheet(button_qss("primary"))
            csv_btn.setStyleSheet(button_qss("secondary"))
            jsonl_btn.setStyleSheet(button_qss("secondary"))
            import_btn.setStyleSheet(button_qss("secondary"))
            reset_btn.setStyleSheet(button_qss("danger"))
            reset_all_btn.setStyleSheet(button_qss("danger"))
            reset_btn.setToolTip("평가 기록만 삭제 (계정·샘플 유지)")
            reset_all_btn.setToolTip(
                "평가 기록 전부 + 평가자 계정 삭제 (샘플·관리자 유지)"
            )
            review_btn.clicked.connect(self.on_review)
            csv_btn.clicked.connect(lambda: self.export("csv"))
            jsonl_btn.clicked.connect(lambda: self.export("jsonl"))
            import_btn.clicked.connect(self.import_results)
            reset_btn.clicked.connect(self.reset_evaluations)
            reset_all_btn.clicked.connect(self.reset_all)
            action_row.addWidget(review_btn)
            action_row.addWidget(csv_btn)
            action_row.addWidget(jsonl_btn)
            action_row.addWidget(import_btn)
            action_row.addWidget(reset_btn)
            action_row.addWidget(reset_all_btn)
            export_actions.addWidget(actions_label)
            export_actions.addLayout(action_row)
            hero_top.addLayout(export_actions)
            hero_layout.addLayout(hero_top)
            self.hero_progress = QProgressBar()
            self.hero_progress.setRange(0, 100)
            self.hero_progress.setTextVisible(True)
            self.hero_progress.setStyleSheet(progress_qss())
            hero_layout.addWidget(self.hero_progress)
            hero_layout.addWidget(self.evaluator_summary)
            layout.addWidget(hero)

            account_box = apply_card(QFrame())
            account_layout = QVBoxLayout(account_box)
            account_layout.setContentsMargins(16, 14, 16, 14)
            account_layout.setSpacing(10)
            account_header = QHBoxLayout()
            account_title = QLabel("Evaluator Accounts")
            account_title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT};")
            self.account_count_label = QLabel()
            self.account_count_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; font-weight: 700;")
            self.add_account_btn = QPushButton("Add evaluator")
            self.add_account_btn.setMinimumHeight(32)
            self.add_account_btn.setStyleSheet(button_qss("primary"))
            self.add_account_btn.clicked.connect(self.open_create_evaluator_dialog)
            self.account_toggle_btn = QPushButton("Hide")
            self.account_toggle_btn.setMinimumHeight(32)
            self.account_toggle_btn.setStyleSheet(button_qss("secondary"))
            self.account_toggle_btn.clicked.connect(self.toggle_account_section)
            account_header.addWidget(account_title)
            account_header.addStretch()
            account_header.addWidget(self.account_count_label)
            account_header.addWidget(self.add_account_btn)
            account_header.addWidget(self.account_toggle_btn)
            self.account_list = QWidget()
            self.account_list_layout = QVBoxLayout(self.account_list)
            self.account_list_layout.setContentsMargins(0, 0, 0, 0)
            self.account_list_layout.setSpacing(8)
            account_layout.addLayout(account_header)
            account_layout.addWidget(self.account_list)

            score_box = apply_card(QFrame())
            score_layout = QVBoxLayout(score_box)
            score_layout.setContentsMargins(16, 14, 16, 14)
            score_layout.setSpacing(10)
            score_header = QVBoxLayout()
            score_title = QLabel("Evaluator Score Summary")
            score_title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT};")
            score_subtitle = QLabel("Completion and average scoring by evaluator")
            score_subtitle.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
            score_header.addWidget(score_title)
            score_header.addWidget(score_subtitle)
            score_layout.addLayout(score_header)
            self.score_cards = QWidget()
            self.score_cards_layout = QGridLayout(self.score_cards)
            self.score_cards_layout.setContentsMargins(0, 0, 0, 0)
            self.score_cards_layout.setHorizontalSpacing(GAP)
            self.score_cards_layout.setVerticalSpacing(GAP)
            self.score_cards.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            score_layout.addWidget(self.score_cards)
            layout.addWidget(score_box)
            layout.addWidget(account_box)

            self.refresh()

        def refresh(self):
            summary = store.progress_summary()
            progress_rows = store.evaluator_progress()
            total_assignments = sum(row["total"] for row in progress_rows)
            completed_assignments = sum(row["completed"] for row in progress_rows)
            evaluator_lines = [
                f"{row['evaluator_id']}: {row['completed']} / {row['total']}"
                for row in progress_rows
            ]
            self.hero_number.setText(
                f"{completed_assignments} / {total_assignments} evaluations complete"
            )
            self.summary.setText(
                f"Total samples: {summary['total_samples']} · Saved evaluations: {summary['completed_evaluations']}"
            )
            self.evaluator_summary.setText("Evaluator progress · " + " · ".join(evaluator_lines))
            self.hero_progress.setValue(
                _progress_percent(completed_assignments, total_assignments)
            )
            self.refresh_account_list()
            self.refresh_score_table()

        def toggle_account_section(self):
            visible = not self.account_list.isVisible()
            self.account_list.setVisible(visible)
            self.account_toggle_btn.setText("Hide" if visible else "Show")

        def refresh_account_list(self):
            users = store.list_users(role="evaluator")
            self.account_count_label.setText(f"Evaluator accounts: {len(users)}")
            _clear_layout(self.account_list_layout)
            if not users:
                self.account_list_layout.addWidget(
                    QLabel("No evaluator accounts yet. Use \"Add evaluator\" to create one.")
                )
                return

            for user in users:
                account_frame = QFrame()
                account_frame.setStyleSheet(
                    f"""
                    QFrame {{
                      background: {PANEL};
                      border: 1px solid {CARD_BORDER};
                      border-radius: 6px;
                    }}
                    QLabel {{ border: 0; background: transparent; }}
                    """
                )
                account_row = QHBoxLayout(account_frame)
                account_row.setContentsMargins(12, 8, 12, 8)
                account_row.setSpacing(8)
                account_label = QLabel(f"{user.name} · {user.user_id}")
                account_label.setStyleSheet(f"color: {TEXT}; font-weight: 600;")
                account_row.addWidget(account_label, 1)
                password_btn = QPushButton("Reset password")
                password_btn.setMinimumHeight(32)
                password_btn.setStyleSheet(button_qss("secondary"))
                password_btn.clicked.connect(
                    lambda _checked=False, account=user: self.reset_evaluator_password(
                        account
                    )
                )
                account_row.addWidget(password_btn)
                delete_btn = QPushButton("Delete")
                delete_btn.setMinimumHeight(32)
                delete_btn.setStyleSheet(button_qss("danger"))
                delete_btn.clicked.connect(
                    lambda _checked=False, user_id=user.user_id: self.delete_evaluator_account(
                        user_id
                    )
                )
                account_row.addWidget(delete_btn)
                self.account_list_layout.addWidget(account_frame)

        def open_create_evaluator_dialog(self):
            dialog = CreateEvaluatorDialog(self)
            if dialog.exec() != QDialog.Accepted or dialog.created_user is None:
                return
            self.refresh()
            QMessageBox.information(
                self,
                "Evaluator created",
                f"Evaluator account '{dialog.created_user.user_id}' was created.\n"
                "Share the ID and password with the evaluator; they can sign in now.",
            )

        def reset_evaluator_password(self, user):
            dialog = PasswordChangeDialog(
                user,
                self,
                title="Reset evaluator password",
                notice=f"Set a new password for evaluator '{user.user_id}'.",
            )
            if dialog.exec() != QDialog.Accepted:
                return
            QMessageBox.information(
                self,
                "Password reset",
                f"The password for '{user.user_id}' was changed.",
            )

        def delete_evaluator_account(self, user_id):
            answer = QMessageBox.question(
                self,
                "Delete evaluator account",
                f"Delete evaluator account '{user_id}' and its evaluations?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

            try:
                store.delete_evaluator_account(user_id)
            except ValueError as exc:
                QMessageBox.warning(self, "Delete failed", str(exc))
                return

            self.refresh()
            QMessageBox.information(self, "Deleted", f"{user_id} was deleted.")

        def refresh_score_table(self):
            rows = store.evaluator_score_summary()
            _clear_layout(self.score_cards_layout)
            for row_index, row in enumerate(rows):
                self.score_cards_layout.addWidget(self.build_score_card(row), 0, row_index)
            self.score_cards_layout.setColumnStretch(len(rows), 1)

        def build_score_card(self, row):
            card = apply_card(QFrame())
            card.setMinimumWidth(EVALUATOR_CARD_MIN_WIDTH)
            card.setMinimumHeight(EVALUATOR_CARD_MIN_HEIGHT)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.setSpacing(10)

            header = QHBoxLayout()
            title = QLabel(row["evaluator_id"])
            title.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {TEXT};")
            done = QLabel(f"{row['completed']} / {row['total']}")
            done.setStyleSheet(f"font-size: 12px; color: {TEXT_MUTED};")
            done.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            header.addWidget(title)
            header.addStretch()
            header.addWidget(done)
            layout.addLayout(header)

            progress = QProgressBar()
            progress.setRange(0, 100)
            progress.setValue(_progress_percent(row["completed"], row["total"]))
            progress.setTextVisible(True)
            progress.setStyleSheet(progress_qss())
            layout.addWidget(progress)


            metrics = QGridLayout()
            metrics.setHorizontalSpacing(12)
            metrics.setVerticalSpacing(6)
            metrics.setColumnStretch(0, 1)
            metrics.setColumnStretch(1, 0)
            for index, (label, key) in enumerate(
                [
                    ("Completeness", "completeness_avg"),
                    ("Correctness", "correctness_avg"),
                    ("Readability", "readability_avg"),
                    ("Overall", "overall_quality_avg"),
                ]
            ):
                metric_label = QLabel(label)
                metric_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
                metric_value = QLabel(_format_score(row[key]))
                metric_value.setAlignment(Qt.AlignCenter)
                metric_value.setMinimumWidth(70)
                metric_value.setStyleSheet(
                    """
                    QLabel {
                      color: #1d232b;
                      background: #eef7f9;
                      border: 1px solid #cfe3e8;
                      border-radius: 6px;
                      padding: 4px 10px;
                      font-weight: 700;
                    }
                    """
                )
                metrics.addWidget(metric_label, index, 0)
                metrics.addWidget(metric_value, index, 1)
            layout.addLayout(metrics)
            return card

        def export(self, kind):
            target, _ = QFileDialog.getSaveFileName(
                self,
                f"Export {kind.upper()}",
                str(store.db_path.parent.parent / "exports" / f"doctor_eval_results.{kind}"),
            )
            if not target:
                return
            rows = store.export_rows()
            try:
                if kind == "csv":
                    write_csv(rows, target)
                else:
                    write_jsonl(rows, target)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Export failed",
                    f"Could not write {kind.upper()} export.\n\n{exc}\n\nNo file was saved at:\n{target}",
                )
                return
            QMessageBox.information(self, "Exported", f"Saved to {target}")

        def import_results(self):
            targets, _ = QFileDialog.getOpenFileNames(
                self,
                "Import evaluator results",
                str(store.db_path.parent.parent / "exports"),
                "JSONL (*.jsonl);;All files (*)",
            )
            if not targets:
                return
            records = []
            try:
                for target in targets:
                    with open(target, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                records.append(json.loads(line))
            except (OSError, ValueError) as exc:
                QMessageBox.critical(
                    self,
                    "Import failed",
                    f"Could not read the selected results file(s).\n\n{exc}",
                )
                return
            try:
                summary = store.import_evaluations(records)
            except ValueError as exc:
                QMessageBox.critical(
                    self,
                    "Import failed",
                    f"The selected results are not in a valid format.\n\n{exc}",
                )
                return
            self.refresh()
            message = (
                f"Imported {summary['imported']} evaluation(s) "
                f"from {len(summary['evaluators'])} evaluator(s)."
            )
            if summary["skipped_missing_sample"]:
                message += (
                    f"\n\nSkipped {summary['skipped_missing_sample']} record(s) "
                    "for samples not in this dataset."
                )
            if summary["skipped_invalid_evaluator"]:
                message += (
                    f"\n\nSkipped {summary['skipped_invalid_evaluator']} record(s) "
                    "whose evaluator ID is invalid, clashes with an existing "
                    "account, or belongs to an admin."
                )
            QMessageBox.information(self, "Import complete", message)

        def reset_evaluations(self):
            answer = QMessageBox.question(
                self,
                "Reset all evaluations",
                (
                    "Delete all saved evaluation records?\n\n"
                    "Evaluator accounts and samples will remain."
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            store.reset_evaluations()
            self.refresh()
            QMessageBox.information(
                self,
                "Reset complete",
                "All evaluation records were deleted. Users and samples remain.",
            )

        def reset_all(self):
            # Destructive and account-level, so confirm in two stages.
            first = QMessageBox.warning(
                self,
                "Full Reset",
                "데이터베이스를 새 평가 라운드용으로 초기화합니다.\n\n"
                "• 모든 평가 기록이 삭제됩니다.\n"
                "• 모든 평가자 계정이 삭제됩니다. "
                "(초기화 후 대시보드에서 새로 만들 수 있습니다.)\n"
                "• 샘플과 관리자 계정은 그대로 유지됩니다.\n\n"
                "초기화 직전 상태는 storage/backups 폴더에 자동 백업됩니다.\n"
                "계속하시겠습니까?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if first != QMessageBox.Yes:
                return
            second = QMessageBox.question(
                self,
                "Full Reset 확인",
                "정말 전체 초기화하시겠습니까?\n"
                "이 작업은 되돌릴 수 없습니다. (백업은 남습니다.)",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if second != QMessageBox.Yes:
                return
            try:
                summary = store.reset_all()
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Full Reset 실패",
                    f"초기화하지 못했습니다.\n\n{exc}",
                )
                return
            # Also set aside any stranded unsaved-work dumps so a fresh round
            # does not resurrect a previous evaluator's drafts. Backed up, not
            # deleted, consistent with how Reset preserves discarded work.
            self._quarantine_stray_dumps()
            self.refresh()
            QMessageBox.information(
                self,
                "Full Reset 완료",
                f"평가 기록 {summary['evaluations_removed']}건과 "
                f"평가자 계정 {summary['evaluator_accounts_removed']}개를 삭제했습니다.\n"
                "Add evaluator 버튼으로 새 평가자 계정을 만들 수 있습니다.",
            )

        def _quarantine_stray_dumps(self):
            storage_dir = store.db_path.parent
            for path in storage_dir.glob("unsaved-*.json"):
                try:
                    path.rename(path.with_suffix(path.suffix + ".discarded"))
                except OSError:
                    pass

    class AdminSampleComparisonReviewView(QWidget):
        def __init__(self, on_back):
            super().__init__()
            self.on_back = on_back
            self.details = store.sample_review_comparison_details()
            self.current_index = 0
            self.reviewer_columns = []
            self.reviewer_status_labels = {}
            self.score_values = {}
            self.completeness_cells = {}
            self.completeness_row_widgets = {}
            self.comment_boxes = {}

            root = QVBoxLayout(self)
            root.setContentsMargins(16, 14, 16, 14)
            root.setSpacing(GAP)
            top = QHBoxLayout()
            title = QLabel("Sample Review Comparison")
            title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {TEXT};")
            top.addWidget(title)
            top.addStretch()
            back_btn = QPushButton("Back to admin")
            back_btn.setMinimumHeight(36)
            back_btn.setStyleSheet(button_qss("secondary"))
            back_btn.clicked.connect(on_back)
            top.addWidget(back_btn)
            root.addLayout(top)

            if not self.details:
                root.addWidget(QLabel("No samples are available for review comparison."))
                root.addStretch()
                return

            self.sample_list = QListWidget()
            self.sample_list.setMaximumWidth(170)
            self.sample_list.setMinimumWidth(130)
            self.sample_list.itemSelectionChanged.connect(self.select_sample)

            nav = QHBoxLayout()
            self.previous_btn = QPushButton("←")
            self.next_btn = QPushButton("→")
            self.sample_counter = QLabel()
            self.sample_counter.setAlignment(Qt.AlignCenter)
            self.sample_counter.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px; font-weight: 700;")
            for button in (self.previous_btn, self.next_btn):
                button.setMinimumHeight(36)
                button.setMinimumWidth(48)
                button.setStyleSheet(button_qss("secondary"))
            self.previous_btn.clicked.connect(self.previous_sample)
            self.next_btn.clicked.connect(self.next_sample)
            nav.addWidget(self.previous_btn)
            nav.addWidget(self.sample_counter, 1)
            nav.addWidget(self.next_btn)

            splitter = QSplitter()
            root.addWidget(splitter)
            splitter.addWidget(self.sample_list)

            reader = QScrollArea()
            reader.setWidgetResizable(True)
            reader_body = QWidget()
            reader_layout = QVBoxLayout(reader_body)
            reader_layout.setSpacing(8)
            self.sample_title = QLabel()
            self.sample_title.setStyleSheet("font-size: 18px; font-weight: 700;")
            self.note = QTextEdit()
            self.note.setReadOnly(True)
            self.note.setMinimumHeight(NOTE_MIN_HEIGHT)
            self.course = QTextEdit()
            self.course.setReadOnly(True)
            self.course.setMinimumHeight(COURSE_MIN_HEIGHT)
            self.course.setMaximumHeight(COURSE_MAX_HEIGHT)
            reader_layout.addWidget(self.sample_title)
            reader_layout.addLayout(nav)
            reader_layout.addWidget(QLabel("Discharge Note"))
            reader_layout.addWidget(self.note, 2)
            reader_layout.addWidget(QLabel("Brief Hospital Course"))
            reader_layout.addWidget(self.course, 0)
            reader.setWidget(reader_body)
            splitter.addWidget(reader)

            comparison = QScrollArea()
            comparison.setWidgetResizable(True)
            comparison_body = QWidget()
            self.comparison_layout = QVBoxLayout(comparison_body)
            self.comparison_layout.setSpacing(GAP)
            comparison_title = QLabel("Reviewer scoring forms")
            comparison_title.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {TEXT};")
            self.comparison_layout.addWidget(comparison_title)
            self.reviewer_grid = QWidget()
            self.reviewer_grid_layout = QHBoxLayout(self.reviewer_grid)
            self.reviewer_grid_layout.setContentsMargins(0, 0, 0, 0)
            self.reviewer_grid_layout.setSpacing(GAP)
            self.comparison_layout.addWidget(self.reviewer_grid)
            self.comparison_layout.addStretch()
            comparison.setWidget(comparison_body)
            splitter.addWidget(comparison)

            splitter.setSizes([130, 300, 860])
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 0)
            splitter.setStretchFactor(2, 2)

            self.refresh_list()
            self.refresh_detail()

        def refresh_list(self):
            self.sample_list.clear()
            for detail in self.details:
                label = f"{detail['sample_order']} · {detail['sample_id']}"
                self.sample_list.addItem(QListWidgetItem(label))
            if self.details:
                self.sample_list.setCurrentRow(self.current_index)

        def select_sample(self):
            row = self.sample_list.currentRow()
            if row >= 0:
                self.current_index = row
                self.refresh_detail()

        def previous_sample(self):
            if self.current_index <= 0:
                return
            self.current_index -= 1
            self.sample_list.blockSignals(True)
            self.sample_list.setCurrentRow(self.current_index)
            self.sample_list.blockSignals(False)
            self.refresh_detail()

        def next_sample(self):
            if self.current_index >= len(self.details) - 1:
                return
            self.current_index += 1
            self.sample_list.blockSignals(True)
            self.sample_list.setCurrentRow(self.current_index)
            self.sample_list.blockSignals(False)
            self.refresh_detail()

        def refresh_detail(self):
            if not self.details:
                return
            detail = self.details[self.current_index]
            self.sample_title.setText(
                f"Sample {detail['sample_id']} · order {detail['sample_order']}"
            )
            self.note.setPlainText(detail["discharge_note"])
            self.course.setPlainText(detail["brief_hospital_course"])
            self.sample_counter.setText(
                f"{self.current_index + 1} / {len(self.details)} · {detail['sample_id']}"
            )
            self.previous_btn.setEnabled(self.current_index > 0)
            self.next_btn.setEnabled(self.current_index < len(self.details) - 1)

            self.reviewer_columns = []
            self.reviewer_status_labels = {}
            self.score_values = {}
            self.reviewer_metric_checks = {}
            self.correctness_count_inputs = {}
            self.overall_inputs = {}
            self.deduction_labels = {}
            self.completeness_cells = {}
            self.completeness_row_widgets = {}
            self.comment_boxes = {}
            self.completeness_disagreement_keys = {
                key
                for key in COMPLETENESS_DEDUCTION_KEYS
                if len(
                    set(
                        key in (reviewer["deductions"] or {}).get("completeness", [])
                        for reviewer in detail["reviewers"]
                        if reviewer["completed"]
                    )
                ) > 1
            }
            _clear_layout(self.reviewer_grid_layout)

            for reviewer in detail["reviewers"]:
                self.reviewer_grid_layout.addWidget(self._reviewer_card(reviewer))
            self.reviewer_grid_layout.addStretch()

        def _reviewer_card(self, reviewer):
            card = apply_card(QFrame())
            card.setMinimumWidth(220)
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            layout = QVBoxLayout(card)
            layout.setContentsMargins(14, 12, 14, 12)
            layout.setSpacing(8)

            evaluator_id = reviewer["evaluator_id"]
            deductions = reviewer["deductions"] or {}
            scores = reviewer["scores"] or {}
            completed = reviewer["completed"]

            title = QLabel(f"{reviewer['name']} · {evaluator_id}")
            title.setWordWrap(True)
            title.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {TEXT};")
            status = QLabel("Completed" if completed else "Not completed")
            status.setObjectName(f"reviewer_status_{evaluator_id}")
            status.setStyleSheet(
                "color: #1d6f8f; font-weight: 800;" if completed else "color: #b42318; font-weight: 800;"
            )
            layout.addWidget(title)
            layout.addWidget(status)
            if not completed:
                pending = QLabel("This reviewer has not submitted this sample yet.")
                pending.setWordWrap(True)
                pending.setStyleSheet(f"color: {TEXT_MUTED};")
                layout.addWidget(pending)
            self.reviewer_status_labels[evaluator_id] = status
            self.score_values[evaluator_id] = {}

            for metric in ("completeness", "correctness", "readability"):
                layout.addWidget(self._metric_group(evaluator_id, metric, completed, scores, deductions))

            layout.addWidget(QLabel("Overall Quality"))
            overall = QComboBox()
            overall.setObjectName(f"overall_{evaluator_id}")
            overall.addItem("Select", "")
            for score, text in ADMIN_OVERALL_QUALITY_OPTIONS:
                overall.addItem(f"{score} · {text}", score)
            if completed:
                self._set_combo_data(overall, scores.get("overall_quality"))
            overall.setEnabled(False)
            layout.addWidget(overall)
            self.score_values[evaluator_id]["overall_quality"] = overall
            self.overall_inputs[evaluator_id] = overall

            layout.addWidget(QLabel("Detailed comment · optional"))
            comment = QTextEdit()
            comment.setReadOnly(True)
            comment.setObjectName(f"comment_{evaluator_id}")
            comment.setMinimumHeight(90)
            comment.setPlainText(reviewer["detailed_comment"] if completed else "")
            layout.addWidget(comment)
            self.comment_boxes[evaluator_id] = comment
            self.reviewer_columns.append(card)
            return card

        def _metric_group(self, evaluator_id, metric, completed, scores, deductions):
            group = QGroupBox(metric.title())
            group_layout = QVBoxLayout(group)
            score_label = QLabel(self._score_label_text(metric, completed, scores, deductions))
            score_label.setObjectName(f"score_{evaluator_id}_{metric}")
            group_layout.addWidget(score_label)
            self.score_values[evaluator_id][metric] = score_label

            if metric == "correctness":
                for key in CORRECTNESS_DEDUCTION_KEYS:
                    counter_row = QHBoxLayout()
                    counter_row.setContentsMargins(0, 0, 0, 0)
                    label = QLabel(ADMIN_DEDUCTION_LABELS["correctness"][key])
                    label.setWordWrap(True)
                    spin = QSpinBox()
                    spin.setRange(0, CORRECTNESS_COUNT_MAX)
                    spin.setSingleStep(1)
                    spin.setValue((deductions.get("correctness") or {}).get(key, 0) if completed else 0)
                    spin.setEnabled(False)
                    spin.setObjectName(f"correctness_{key}_{evaluator_id}")
                    counter_row.addWidget(label, 1)
                    counter_row.addWidget(spin)
                    group_layout.addLayout(counter_row)
                    self.correctness_count_inputs[(evaluator_id, key)] = spin
                return group

            for key, text in EVALUATION_DEDUCTION_OPTIONS[metric]:
                selected = completed and key in deductions.get(metric, [])
                is_disagreement = (
                    metric == "completeness" and key in self.completeness_disagreement_keys
                )
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(6, 3, 6, 3)
                row_layout.setSpacing(8)
                check = QCheckBox()
                check.setChecked(selected)
                check.setObjectName(f"{metric}_{key}_{evaluator_id}")
                check.setFocusPolicy(Qt.NoFocus)
                if completed:
                    # Read-only but rendered at full contrast so selections stay legible.
                    check.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                else:
                    check.setEnabled(False)
                    check.setToolTip("Not completed")
                label = QLabel(ADMIN_DEDUCTION_LABELS[metric].get(key, text))
                label.setWordWrap(True)
                if not completed:
                    label.setStyleSheet(f"color: {TEXT_MUTED};")
                    label.setToolTip("Not completed")
                elif selected:
                    label.setStyleSheet(f"color: {TEXT}; font-weight: 800;")
                else:
                    label.setStyleSheet(f"color: {TEXT_MUTED};")
                if is_disagreement:
                    row.setStyleSheet(
                        "QWidget { background: #fff4d6; border: 1px solid #f2c94c; border-radius: 4px; }"
                    )
                elif selected:
                    row.setStyleSheet(
                        f"QWidget {{ background: #dceef3; border: 1px solid {ACCENT}; border-radius: 4px; }}"
                    )
                row_layout.addWidget(check)
                row_layout.addWidget(label, 1)
                group_layout.addWidget(row)
                self.reviewer_metric_checks[(evaluator_id, metric, key)] = check
                self.deduction_labels[(evaluator_id, metric, key)] = label
                if metric == "completeness":
                    self.completeness_cells[(key, evaluator_id)] = check
                    self.completeness_row_widgets.setdefault(key, []).append(row)
            return group

        def _score_label_text(self, metric, completed, scores, deductions):
            if not completed:
                if metric == "correctness":
                    return "Score: — · Errors: — · max -4"
                return "Score: —"
            score = scores.get(metric)
            display_score = score if score is not None else "—"
            if metric == "correctness":
                error_count = sum((deductions.get("correctness") or {}).values())
                return f"Score: {display_score} · Errors: {error_count} · max -4"
            return f"Score: {display_score}"

        def _set_combo_data(self, combo, value):
            for index in range(combo.count()):
                if combo.itemData(index) == value:
                    combo.setCurrentIndex(index)
                    return

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Doctor Evaluation")
            self.resize(1220, 760)
            self.stack = QStackedWidget()
            self.setCentralWidget(self.stack)
            self.current_user = None
            self.login = LoginView(self.show_for_user)
            self.stack.addWidget(self.login)
            # First launch: no admin yet, so the app opens on admin setup
            # instead of a login screen nobody could get past.
            self.setup = None
            if not store.has_admin():
                self.setup = AdminSetupView(self.finish_admin_setup)
                self.stack.addWidget(self.setup)
                self.stack.setCurrentWidget(self.setup)

        def finish_admin_setup(self, admin_id):
            # show_login drops every non-login widget from the stack, which
            # disposes of the setup screen as well.
            self.setup = None
            self.show_login()
            self.login.prefill(admin_id)

        def show_for_user(self, user):
            # Everything the session does from here runs under this identity.
            store.set_actor(user)
            self.current_user = user
            if user.must_change_password:
                PasswordChangeDialog(user, self).exec()
                # Decide on the stored state, not on how the dialog closed: a
                # dialog that reports success without a rotation actually having
                # landed must not admit the session.
                if store.password_change_required(user.user_id):
                    self.show_login()
                    return
                user = replace(user, must_change_password=False)
                store.set_actor(user)
                self.current_user = user
            if user.role == "admin":
                self.show_admin()
                return
            self.show_evaluator(user)

        def show_evaluator(self, user):
            view = EvaluatorView(user, self.show_login)
            self.stack.addWidget(view)
            self.stack.setCurrentWidget(view)

        def _admin_user(self):
            """Gate admin screens on the authenticated identity, not on which
            screen happened to invoke them."""
            user = self.current_user
            if user is None or user.role != "admin":
                self.show_login()
                return None
            return user

        def show_admin(self):
            if self._admin_user() is None:
                return
            view = AdminView(
                self.show_login,
                self.show_admin_review,
            )
            self.stack.addWidget(view)
            self.stack.setCurrentWidget(view)

        def show_admin_review(self):
            if self._admin_user() is None:
                return
            view = AdminSampleComparisonReviewView(self.show_admin)
            self.stack.addWidget(view)
            self.stack.setCurrentWidget(view)

        def show_login(self):
            # Drop the authority the session was running under.
            store.set_actor(None)
            self.current_user = None
            self.stack.setCurrentWidget(self.login)
            # Session views hold the sample's clinical text in widget state.
            # They were previously left in the stack after logout, so the next
            # user's process still had the previous user's notes in memory.
            for index in reversed(range(self.stack.count())):
                widget = self.stack.widget(index)
                if widget is self.login:
                    continue
                self.stack.removeWidget(widget)
                widget.deleteLater()

        def closeEvent(self, event):
            current = self.stack.currentWidget()
            if isinstance(current, EvaluatorView) and not current.confirm_app_close():
                event.ignore()
                return
            event.accept()

    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    return app.exec()


def _iso(value):
    return value.isoformat() if value else None


def _format_score(value):
    if value is None:
        return "-"
    return f"{value:.2f}"




def _progress_percent(completed, total):
    if not total:
        return 0
    return round((completed / total) * 100)


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
            continue
        child_layout = item.layout()
        if child_layout is not None:
            _clear_layout(child_layout)
