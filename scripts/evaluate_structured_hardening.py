#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Iterable

from datasets import load_dataset

from fast_translate import Translator


FENCED_CODE_RE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?\n\2(?=\n|$)", flags=re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
LATEX_RE = re.compile(
    r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)|\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|eqnarray\*?|math|displaymath)\}.*?\\end\{\1\}",
    flags=re.DOTALL,
)


def extract_code_spans(text: str) -> list[str]:
    spans = [m.group(0) for m in FENCED_CODE_RE.finditer(text)]
    spans.extend(m.group(0) for m in INLINE_CODE_RE.finditer(text))
    return spans


def extract_latex_spans(text: str) -> list[str]:
    return [m.group(0) for m in LATEX_RE.finditer(text)]


def has_structured(text: str) -> bool:
    return bool(extract_code_spans(text) or extract_latex_spans(text))


def gather_ultrachat_samples(limit: int) -> list[str]:
    ds = load_dataset("HuggingFaceH4/ultrachat_200k", split="train_sft", streaming=True)
    out: list[str] = []
    for row in ds:
        messages = row.get("messages") or []
        chunks: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content", "")).strip()
            if content:
                chunks.append(content)
        if not chunks:
            continue
        text = "\n\n".join(chunks)
        if has_structured(text):
            out.append(text)
        if len(out) >= limit:
            break
    return out


def gather_numina_samples(limit: int) -> list[str]:
    ds = load_dataset("AI-MO/NuminaMath-CoT", split="train", streaming=True)
    out: list[str] = []
    for row in ds:
        problem = str(row.get("problem", "")).strip()
        solution = str(row.get("solution", "")).strip()
        text = "\n\n".join([p for p in [problem, solution] if p])
        if not text:
            continue
        if has_structured(text):
            out.append(text)
        if len(out) >= limit:
            break
    return out


def gather_codesearchnet_samples(limit: int) -> list[str]:
    ds = load_dataset("code_search_net", "python", split="train", streaming=True)
    out: list[str] = []
    for row in ds:
        doc = str(row.get("func_documentation_string", "")).strip()
        code = str(row.get("func_code_string", "")).strip()
        if not doc or not code:
            continue
        code = code[:800]
        text = f"{doc}\n\n```python\n{code}\n```"
        out.append(text)
        if len(out) >= limit:
            break
    return out


def gather_pt_samples(limit: int) -> list[str]:
    ds = load_dataset("orion-research/little-stories-en_US-pt_BR", split="train", streaming=True)
    templates = [
        "Trecho técnico:\n```python\nfor i in range(3):\n    print(i)\n```\nExplique sem alterar o código.",
        "Considere a fórmula: $$E = mc^2$$ e descreva em linguagem simples.",
        "Mantenha `pip install fast-translate` igual e traduza apenas o restante.",
        "Aqui está um bloco:\n\\begin{equation}\n\\int_0^1 x^2 dx = 1/3\n\\end{equation}\nContinue em texto.",
    ]
    out: list[str] = []
    idx = 0
    for row in ds:
        base = str(row.get("output", "")).strip()
        if not base:
            continue
        mixed = f"{base}\n\n{templates[idx % len(templates)]}"
        idx += 1
        if has_structured(mixed):
            out.append(mixed)
        if len(out) >= limit:
            break
    return out


def evaluate_direction(tr: Translator, texts: Iterable[str], direction: str) -> dict[str, float | int]:
    total = 0
    code_exact = 0
    latex_exact = 0
    both_exact = 0

    for src in texts:
        total += 1
        out = tr.translate(src, direction=direction)
        src_code = extract_code_spans(src)
        out_code = extract_code_spans(out)
        src_latex = extract_latex_spans(src)
        out_latex = extract_latex_spans(out)

        code_ok = src_code == out_code
        latex_ok = src_latex == out_latex
        if code_ok:
            code_exact += 1
        if latex_ok:
            latex_exact += 1
        if code_ok and latex_ok:
            both_exact += 1

    if total == 0:
        return {
            "total": 0,
            "code_exact_rate": 0.0,
            "latex_exact_rate": 0.0,
            "combined_exact_rate": 0.0,
        }
    return {
        "total": total,
        "code_exact_rate": code_exact / total,
        "latex_exact_rate": latex_exact / total,
        "combined_exact_rate": both_exact / total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate structured-translation hardening (code + LaTeX preservation).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--limit-en", type=int, default=120)
    parser.add_argument("--limit-pt", type=int, default=120)
    parser.add_argument("--output", default="artifacts/metrics_structured_hardening.json")
    args = parser.parse_args()

    random.seed(args.seed)

    # Diverse EN sources: chat markdown, math CoT, and pure code datasets.
    en_texts = []
    en_texts.extend(gather_ultrachat_samples(max(20, args.limit_en // 3)))
    en_texts.extend(gather_numina_samples(max(20, args.limit_en // 3)))
    en_texts.extend(gather_codesearchnet_samples(max(20, args.limit_en // 3)))
    random.shuffle(en_texts)
    en_texts = en_texts[: args.limit_en]

    # PT sources: PT-BR dataset with injected structured blocks.
    pt_texts = gather_pt_samples(args.limit_pt)
    random.shuffle(pt_texts)
    pt_texts = pt_texts[: args.limit_pt]

    with Translator() as tr:
        en_metrics = evaluate_direction(tr, en_texts, "en-pt")
        pt_metrics = evaluate_direction(tr, pt_texts, "pt-en")

    report = {
        "datasets": {
            "en": [
                "HuggingFaceH4/ultrachat_200k (train_sft)",
                "AI-MO/NuminaMath-CoT (train)",
                "code_search_net/python (train)",
            ],
            "pt": [
                "orion-research/little-stories-en_US-pt_BR (train, output column + structured templates)",
            ],
        },
        "limits": {"en": len(en_texts), "pt": len(pt_texts)},
        "metrics": {"en_pt": en_metrics, "pt_en": pt_metrics},
        "target_combined_exact_rate": 0.99,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
