"""Generate one Brief Hospital Course per discharge note with the base model + LoRA adapter (vLLM).

The prompt is rendered exactly as in training (train_bhc_generator.render_prompt). Sampling is fixed to the
study setting: temperature 0.7, top-p 0.9, repetition penalty 1.05, seed 42; greedy decoding
looped or copied the note in some outputs. Outputs cut at max_tokens are kept and flagged
by finish_reason, not regenerated.

    python bhc_generator/generate_bhc.py --model Qwen/Qwen3-8B --adapter outputs/bhc_generator \
        --data train/data/mock_sft.jsonl --output outputs/bhc_generator/generations.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_bhc_generator import DEFAULT_DATA, DEFAULT_MODEL, SEED, read_jsonl, render_prompt  # noqa: E402

SAMPLING = dict(temperature=0.7, top_p=0.9, repetition_penalty=1.05, max_tokens=3072)
MAX_MODEL_LEN = 16384


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="base model the adapter was trained on")
    parser.add_argument("--adapter", type=Path, required=True, help="output directory of train_bhc_generator.py")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="JSONL with id and discharge_note")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    from transformers import AutoTokenizer
    from vllm import LLM, SamplingParams
    from vllm.lora.request import LoRARequest

    rows = read_jsonl(args.data)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    prompts = [render_prompt(tokenizer, row["discharge_note"]) for row in rows]
    lora_rank = json.loads((args.adapter / "adapter_config.json").read_text())["r"]

    llm = LLM(model=args.model, dtype="bfloat16", seed=SEED, max_model_len=MAX_MODEL_LEN,
              enable_lora=True, max_lora_rank=lora_rank, enable_prefix_caching=False)
    results = llm.generate(prompts, SamplingParams(seed=SEED, **SAMPLING),
                           lora_request=LoRARequest("bhc_generator", 1, str(args.adapter)))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        for row, result in zip(rows, results):
            out = result.outputs[0]
            handle.write(json.dumps({"id": row["id"], "generated_bhc": out.text.strip(),
                                     "finish_reason": out.finish_reason}, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} generations to {args.output}")


if __name__ == "__main__":
    main()
