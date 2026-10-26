# Prompts

| File | Condition in the paper | Original file name | SHA-256 |
|---|---|---|---|
| `base.md` | Base prompt, deduction-based evaluation | `doctor_eval.base.v2.md` | `f92b6f6edf280c7612e3a2c89512c53339114a9266cd00d2e4b2b0c35f44063f` |
| `refined.md` | Refined prompt, deduction-based evaluation (also used at inference for the SFT and SFT + DPO models) | `doctor_eval.e017_abbr_threshold.md` | `3cbe49f234391d5841b22aa3138165db6314ec3f9b6a6ef164de83f54be4d3b7` |
| `scoring.md` | Direct-scoring prompt | `doctor_eval.base.score.md` | `f88f2c284491bac6cafff8575a08c679c822af999c5b3251ae3634ae5619ffd5` |

The files are unchanged copies of the prompt files used in the study.

- At inference, two HTML comment lines recording the output format and the SHA-256 of the source file were prepended to each prompt.
- The INPUT section is not part of these files. It was appended at inference as a JSON object with two keys: `reference_note_kr` (the discharge note with its Brief Hospital Course removed) and `candidate_bhc_kr` (the candidate Brief Hospital Course).
- In deduction-based evaluation, scores were calculated from the returned deduction codes: each domain starts at 5 and loses 1 point per deduction (minimum 1), and overall quality is 5 minus the total number of deductions (minimum 1). The overall quality code returned by the model was not used.
