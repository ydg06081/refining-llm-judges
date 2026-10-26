# Fine-tuning (LoRA SFT → DPO)

Simplified code for the SFT and SFT + DPO judge models. The model learns to return the
deduction codes as JSON; scores are then derived from the codes (see [`../prompts/README.md`](../prompts/README.md)).

The data in `data/` are **mock data** with fictional cases, so you can check that the pipeline runs.
They are not the study data, and their labels are illustrative.

| File | Contents |
|---|---|
| `train_sft.py` | LoRA SFT; the loss covers only the answer tokens |
| `train_dpo.py` | DPO from the SFT adapter; the reference policy is the SFT adapter itself |
| `common.py` | Fixed hyperparameters, input formatting, model loading, adapter saving |
| `accelerate_fsdp.yaml` | 8-GPU FSDP launch config (single machine, bf16) |
| `data/mock_sft.jsonl`, `data/mock_dpo.jsonl` | Mock training data |

## Run

```bash
pip install -r train/requirements.txt   # install a CUDA build of torch first

accelerate launch --config_file train/accelerate_fsdp.yaml train/train_sft.py \
    --model Qwen/Qwen3.6-27B --prompt prompts/refined.md \
    --data train/data/mock_sft.jsonl --output-dir outputs/sft

accelerate launch --config_file train/accelerate_fsdp.yaml train/train_dpo.py \
    --model Qwen/Qwen3.6-27B --sft-adapter outputs/sft --prompt prompts/refined.md \
    --data train/data/mock_dpo.jsonl --output-dir outputs/dpo
```

`--model` takes any Hugging Face model id or local path, and `--prompt` takes any file in `prompts/`.
With another model family, set `fsdp_transformer_layer_cls_to_wrap` in `accelerate_fsdp.yaml`
to that model's decoder-layer class (for example `Gemma4TextDecoderLayer`).
The scripts also run without `accelerate launch`, as a single process.

## Data format

One JSON object per line. `target`, `chosen` and `rejected` contain the deduction codes:

```json
{"id": "mock_02",
 "discharge_note": "Chief Complaint: ... (discharge note with the BHC section removed)",
 "candidate_bhc": "candidate Brief Hospital Course",
 "target": {"completeness_deductions": ["COMP_DISCHARGE"],
            "correctness_deductions": [],
            "readability_deductions": ["READ_ABBR"]}}
```

For DPO, replace `target` with `chosen` (the preferred output) and `rejected`.

Each example is formatted as two chat turns:

- system: the prompt file, followed by a fine-tuning instruction (`OUTPUT_OVERRIDE` in `common.py`)
  to return only the three deduction arrays
- user: `## INPUT\nDischarge Note:\n<discharge_note>\n\nCandidate BHC:\n<candidate_bhc>\n`

The assistant answer is the label JSON (indent 2). Qwen3.x chat templates use non-thinking mode.

## Fixed hyperparameters (`common.py`)

| | SFT | DPO |
|---|---|---|
| Learning rate | 3e-4 | 1e-5 |
| Epochs | 3 | 1 |
| β | – | 0.05 |
| Schedule | cosine, warmup 3% | cosine, warmup 3% |

Common to both: LoRA r 16, α 32, dropout 0.05 on the q/k/v/o/gate/up/down projections; AdamW
(β 0.9/0.999, weight decay 0.01); batch size 1 per GPU; no gradient accumulation; bf16;
gradient checkpointing; maximum sequence length 8192 tokens (longer examples raise an error
instead of being truncated); seed 42.
