# Medical LLM Judge

Prompts and evaluation application accompanying the manuscript "Refining large language model judges of discharge summaries with physician disagreements".

| Folder | Contents |
|---|---|
| [`prompts/`](prompts/) | Base and Refined prompts for deduction-based evaluation, and the direct-scoring prompt |
| [`evaluation_app/`](evaluation_app/) | Source code of the offline application the physicians used to evaluate the Brief Hospital Course summaries (Python 3.14, PySide6 6.11, SQLite); see its README |

No patient data are included. The clinical records, physician assessments and model outputs cannot be shared (see the Data availability statement of the paper). The sample dataset in `evaluation_app/data/` is synthetic.
