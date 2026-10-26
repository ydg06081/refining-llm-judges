from dataclasses import dataclass, field


@dataclass(frozen=True)
class Sample:
    sample_id: str
    discharge_note: str
    brief_hospital_course: str
    llm_judge: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class User:
    user_id: str
    role: str
    name: str = ""
    status: str = "approved"
    must_change_password: bool = False
