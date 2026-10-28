"""LoRA SFT of a Brief Hospital Course generator (discharge note -> BHC) on Discharge Me!.

The prompt is the chat template applied to one user turn (instruction + discharge note) with
the generation prompt and non-thinking mode, i.e. what vLLM renders in generate_bhc.py. The
completion is the target BHC followed by the end-of-turn token; the loss covers only the
completion tokens. Examples longer than MAX_SEQ_LEN are dropped (and counted), never truncated.

    torchrun --nproc-per-node 8 bhc_generator/train_bhc_generator.py \
        --model Qwen/Qwen3-8B --data train/data/mock_sft.jsonl --output-dir outputs/bhc_generator
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

DEFAULT_MODEL = "Qwen/Qwen3-8B"
DEFAULT_DATA = Path(__file__).resolve().parent.parent / "train" / "data" / "mock_sft.jsonl"
INSTRUCTION = ("Write the Brief Hospital Course section of the discharge summary for the following "
               "discharge note. Output only the Brief Hospital Course text.\n\n")

# Values used in the study (Qwen3-8B, 8x RTX A6000, DDP).
SEED = 42
MAX_SEQ_LEN = 8192
LORA = dict(r=16, lora_alpha=32, lora_dropout=0.05,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
TRAINING = dict(learning_rate=1e-4, num_train_epochs=1, per_device_train_batch_size=1,
                gradient_accumulation_steps=2, lr_scheduler_type="cosine", warmup_ratio=0.03)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def render_prompt(tokenizer, discharge_note: str) -> str:
    return tokenizer.apply_chat_template([{"role": "user", "content": INSTRUCTION + discharge_note}],
                                         tokenize=False, add_generation_prompt=True, enable_thinking=False)


def encode(tokenizer, rows: list[dict]) -> tuple[list[dict], int]:
    examples, dropped = [], 0
    for row in rows:
        prompt = tokenizer(render_prompt(tokenizer, row["discharge_note"]), add_special_tokens=False)["input_ids"]
        answer = tokenizer(row["candidate_bhc"] + tokenizer.eos_token, add_special_tokens=False)["input_ids"]
        if len(prompt) + len(answer) > MAX_SEQ_LEN:
            dropped += 1
            continue
        examples.append({"input_ids": prompt + answer, "labels": [-100] * len(prompt) + answer})
    return examples, dropped


def collate(batch: list[dict], pad_id: int) -> dict:
    n = max(len(b["input_ids"]) for b in batch)
    ids = torch.full((len(batch), n), pad_id)
    labels = torch.full((len(batch), n), -100)
    mask = torch.zeros((len(batch), n), dtype=torch.long)
    for i, b in enumerate(batch):
        k = len(b["input_ids"])
        ids[i, :k], labels[i, :k], mask[i, :k] = torch.tensor(b["input_ids"]), torch.tensor(b["labels"]), 1
    return {"input_ids": ids, "labels": labels, "attention_mask": mask}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Hugging Face model id or local path")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="training JSONL")
    parser.add_argument("--val-data", type=Path, help="optional validation JSONL (evaluated every 50 steps)")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/bhc_generator"))
    args = parser.parse_args()

    from peft import LoraConfig, get_peft_model

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    train, dropped = encode(tokenizer, read_jsonl(args.data))
    val = encode(tokenizer, read_jsonl(args.val_data))[0] if args.val_data else None
    print(f"{len(train)} training examples, {dropped} dropped (> {MAX_SEQ_LEN} tokens)")

    model = AutoModelForCausalLM.from_pretrained(args.model, dtype=torch.bfloat16, attn_implementation="sdpa")
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model = get_peft_model(model, LoraConfig(task_type="CAUSAL_LM", bias="none", **LORA))

    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(args.output_dir / "trainer"), bf16=True, seed=SEED, data_seed=SEED,
            logging_steps=5, eval_strategy="steps" if val else "no", eval_steps=50, per_device_eval_batch_size=1,
            save_strategy="no", report_to=[], remove_unused_columns=False, ddp_find_unused_parameters=False,
            **TRAINING),
        train_dataset=train, eval_dataset=val,
        data_collator=lambda batch: collate(batch, tokenizer.pad_token_id),
    )
    trainer.train()
    if val:
        trainer.evaluate()
    if trainer.is_world_process_zero():
        model.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        print(f"saved adapter to {args.output_dir}")


if __name__ == "__main__":
    main()
