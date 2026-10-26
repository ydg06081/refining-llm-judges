"""Shared pieces of the LoRA SFT and DPO scripts.

Hyperparameters are fixed to the values used in the study (Qwen3.6-27B, 8x RTX A6000).
Only the model, the prompt, the data and the output directory are command-line arguments.
"""
from __future__ import annotations

import json
import math
import random
from pathlib import Path

import torch

DEFAULT_MODEL = "Qwen/Qwen3.6-27B"
DEFAULT_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "refined.md"

SEED = 42
MAX_SEQ_LEN = 8192
LORA = dict(r=16, lora_alpha=32, lora_dropout=0.05, bias="none")
# Attention and MLP projections of every decoder layer (text tower only).
LORA_TARGET_MODULES = r".*\.layers\.\d+\..*\.(q_proj|k_proj|v_proj|o_proj|gate_proj|up_proj|down_proj)$"
ADAMW = dict(betas=(0.9, 0.999), eps=1e-8, weight_decay=0.01)
WARMUP_RATIO = 0.03
SFT = dict(learning_rate=3e-4, epochs=3)
DPO = dict(learning_rate=1e-5, epochs=1, beta=0.05)

# Appended to the system prompt during fine-tuning: the model is trained to emit only the
# deduction codes, and scores are derived from them (see prompts/README.md).
OUTPUT_OVERRIDE = (
    "Fine-tuning output override (compact_three_key.v1): return exactly one JSON object "
    "with only completeness_deductions, correctness_deductions, and readability_deductions. "
    "Emit no score, explanation, object-valued deduction, deduction description, or additional key."
)


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def render_target(value) -> str:
    """Deduction JSON as the model is trained to write it (a string is used verbatim)."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def build_messages(system_prompt: str, row: dict) -> list[dict]:
    system = system_prompt.rstrip() + "\n\n" + OUTPUT_OVERRIDE
    user = (
        "## INPUT\n"
        f"Discharge Note:\n{row['discharge_note']}\n\n"
        f"Candidate BHC:\n{row['candidate_bhc']}\n"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def chat_ids(tokenizer, messages: list[dict], add_generation_prompt: bool) -> list[int]:
    kwargs = {}
    # Qwen3.x templates open a reasoning turn by default; train in non-thinking mode.
    if "enable_thinking" in (tokenizer.chat_template or ""):
        kwargs["enable_thinking"] = False
    ids = tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=add_generation_prompt, **kwargs
    )
    if hasattr(ids, "keys"):
        ids = ids["input_ids"]
    return list(ids)


def encode(tokenizer, messages: list[dict], answer: str) -> tuple[list[int], int]:
    """Token ids of prompt + answer, and the number of answer tokens at the end."""
    prompt = chat_ids(tokenizer, messages, add_generation_prompt=True)
    full = chat_ids(tokenizer, messages + [{"role": "assistant", "content": answer}], False)
    boundary = next((i for i, (a, b) in enumerate(zip(prompt, full)) if a != b), len(prompt))
    if len(full) > MAX_SEQ_LEN:
        # Truncating would cut the rubric out of the prompt; fail instead.
        raise ValueError(f"example needs {len(full)} tokens > MAX_SEQ_LEN={MAX_SEQ_LEN}")
    if boundary >= len(full):
        raise ValueError("answer produced no tokens")
    return full, len(full) - boundary


def answer_logits(model, ids: list[int], n_answer: int, device) -> tuple[torch.Tensor, torch.Tensor]:
    """Logits that predict the answer tokens, and those target tokens."""
    inputs = torch.tensor([ids], device=device)
    # Only the last n_answer + 1 positions are needed; skips the full-vocab projection
    # over the long prompt.
    logits = model(input_ids=inputs, attention_mask=torch.ones_like(inputs),
                   logits_to_keep=n_answer + 1).logits[0, :-1].float()
    return logits, inputs[0, -n_answer:]


def load_model(name: str, adapter: str | None = None):
    from peft import LoraConfig, PeftModel, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(name)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.bfloat16)
    model.config.use_cache = False
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, is_trainable=True)
    else:
        model = get_peft_model(model, LoraConfig(task_type="CAUSAL_LM",
                                                 target_modules=LORA_TARGET_MODULES, **LORA))
    # HF reentrant checkpointing; the accelerate FSDP config leaves its own checkpointing off.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": True})
    model.enable_input_require_grads()
    return model, tokenizer


def prepare(accelerator, model):
    from accelerate.utils import DistributedType

    if accelerator.distributed_type == DistributedType.FSDP:
        from peft.utils.other import fsdp_auto_wrap_policy

        # Gives the fp32 trainable LoRA weights their own FSDP units, separate from the
        # bf16 base weights (FSDP flattens only tensors of one dtype together).
        accelerator.state.fsdp_plugin.auto_wrap_policy = fsdp_auto_wrap_policy(model)
    return accelerator.prepare_model(model)


def make_optimizer(model, learning_rate: float, total_updates: int):
    from transformers import get_scheduler

    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad],
                                  lr=learning_rate, **ADAMW)
    warmup = 0 if total_updates <= 1 else math.ceil(total_updates * WARMUP_RATIO)
    scheduler = get_scheduler("cosine", optimizer, num_warmup_steps=warmup,
                              num_training_steps=total_updates)
    return optimizer, scheduler


def shard(items: list, accelerator) -> list[tuple[object, bool]]:
    """This rank's items, padded so every rank runs the same number of steps.

    Padding items repeat a real one but are flagged so their loss is multiplied by 0.
    """
    world, rank = accelerator.num_processes, accelerator.process_index
    steps = math.ceil(len(items) / world)
    out = []
    for step in range(steps):
        index = step * world + rank
        out.append((items[index], True) if index < len(items) else (items[0], False))
    return out


def real_count(step: int, n_items: int, world: int) -> int:
    """Number of real (non-padding) items across all ranks at this step."""
    return min(world, n_items - step * world)


def save_adapter(accelerator, model, tokenizer, out_dir: Path) -> None:
    state = accelerator.get_state_dict(model)  # gathers the full state under FSDP
    if accelerator.is_main_process:
        out_dir.mkdir(parents=True, exist_ok=True)
        accelerator.unwrap_model(model).save_pretrained(out_dir, state_dict=state)
        tokenizer.save_pretrained(out_dir)
    accelerator.wait_for_everyone()
