"""LoRA SFT of a judge model on physician deduction labels (loss on the answer tokens only).

    accelerate launch --config_file train/accelerate_fsdp.yaml train/train_sft.py \
        --model Qwen/Qwen3.6-27B --prompt prompts/refined.md \
        --data train/data/mock_sft.jsonl --output-dir outputs/sft
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import torch.nn.functional as F
from accelerate import Accelerator

import common


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=common.DEFAULT_MODEL, help="Hugging Face model id or local path")
    parser.add_argument("--prompt", type=Path, default=common.DEFAULT_PROMPT, help="system prompt file")
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data" / "mock_sft.jsonl")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/sft"))
    args = parser.parse_args()

    accelerator = Accelerator()
    common.set_seed(common.SEED)
    model, tokenizer = common.load_model(args.model)
    system_prompt = args.prompt.read_text(encoding="utf-8")
    examples = [
        common.encode(tokenizer, common.build_messages(system_prompt, row), common.render_target(row["target"]))
        for row in common.read_jsonl(args.data)
    ]

    model = common.prepare(accelerator, model)
    steps_per_epoch = math.ceil(len(examples) / accelerator.num_processes)
    total_updates = steps_per_epoch * common.SFT["epochs"]
    optimizer, scheduler = common.make_optimizer(model, common.SFT["learning_rate"], total_updates)
    optimizer = accelerator.prepare_optimizer(optimizer)

    model.train()
    update = 0
    for epoch in range(common.SFT["epochs"]):
        for step, ((ids, n_answer), real) in enumerate(common.shard(examples, accelerator)):
            logits, targets = common.answer_logits(model, ids, n_answer, accelerator.device)
            loss = F.cross_entropy(logits, targets)
            # Gradients are averaged over ranks; rescale so this is the mean over real items.
            n_real = common.real_count(step, len(examples), accelerator.num_processes)
            accelerator.backward(loss * (accelerator.num_processes / n_real) if real else loss * 0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
            update += 1
            accelerator.print(f"epoch {epoch + 1} update {update}/{total_updates} loss {loss.item():.4f}")

    common.save_adapter(accelerator, model, tokenizer, args.output_dir)
    accelerator.print(f"saved adapter to {args.output_dir}")


if __name__ == "__main__":
    main()
