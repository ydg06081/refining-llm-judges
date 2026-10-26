"""DPO on top of the SFT adapter (the reference policy is the SFT adapter itself).

    accelerate launch --config_file train/accelerate_fsdp.yaml train/train_dpo.py \
        --model Qwen/Qwen3.6-27B --sft-adapter outputs/sft --prompt prompts/refined.md \
        --data train/data/mock_dpo.jsonl --output-dir outputs/dpo
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import torch
from accelerate import Accelerator

import common


def sequence_logp(model, example, device) -> torch.Tensor:
    ids, n_answer = example
    logits, targets = common.answer_logits(model, ids, n_answer, device)
    return torch.gather(logits.log_softmax(-1), -1, targets[:, None]).sum()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=common.DEFAULT_MODEL, help="Hugging Face model id or local path")
    parser.add_argument("--sft-adapter", required=True, help="adapter directory written by train_sft.py")
    parser.add_argument("--prompt", type=Path, default=common.DEFAULT_PROMPT, help="system prompt file")
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "data" / "mock_dpo.jsonl")
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/dpo"))
    args = parser.parse_args()

    accelerator = Accelerator()
    common.set_seed(common.SEED)
    model, tokenizer = common.load_model(args.model, adapter=args.sft_adapter)
    system_prompt = args.prompt.read_text(encoding="utf-8")
    pairs = []
    for row in common.read_jsonl(args.data):
        messages = common.build_messages(system_prompt, row)
        pairs.append(tuple(common.encode(tokenizer, messages, common.render_target(row[side]))
                           for side in ("chosen", "rejected")))

    model = common.prepare(accelerator, model)
    world, device, beta = accelerator.num_processes, accelerator.device, common.DPO["beta"]
    my_pairs = common.shard(pairs, accelerator)

    # The policy starts as the SFT adapter, so the reference log-probs are the policy's
    # own before any update. Scoring them once here removes the need for a second model.
    model.eval()
    with torch.no_grad():
        reference = [sequence_logp(model, c, device) - sequence_logp(model, r, device) for (c, r), _ in my_pairs]

    steps_per_epoch = math.ceil(len(pairs) / world)
    total_updates = steps_per_epoch * common.DPO["epochs"]
    optimizer, scheduler = common.make_optimizer(model, common.DPO["learning_rate"], total_updates)
    optimizer = accelerator.prepare_optimizer(optimizer)

    update = 0
    for epoch in range(common.DPO["epochs"]):
        for step, ((chosen, rejected), real) in enumerate(my_pairs):
            # loss = -log sigmoid(z), z = beta * (policy margin - reference margin).
            # Holding the chosen and rejected graphs together does not fit in 48 GB at
            # 8192 tokens, so take dloss/dz = -beta * sigmoid(-z) from a no-grad pass and
            # backpropagate each side separately.
            model.eval()
            with torch.no_grad():
                z = beta * (sequence_logp(model, chosen, device) - sequence_logp(model, rejected, device) - reference[step])
                loss = -torch.nn.functional.logsigmoid(z)
            coef = -beta * torch.sigmoid(-z)
            n_real = common.real_count(step, len(pairs), world)
            scale = world / n_real if real else 0.0
            model.train()
            for sign, example in ((1.0, chosen), (-1.0, rejected)):
                accelerator.backward(sign * coef * scale * sequence_logp(model, example, device))
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
            update += 1
            accelerator.print(f"epoch {epoch + 1} update {update}/{total_updates} loss {loss.item():.4f} margin {z.item() / beta:+.3f}")

    common.save_adapter(accelerator, model, tokenizer, args.output_dir)
    accelerator.print(f"saved adapter to {args.output_dir}")


if __name__ == "__main__":
    main()
