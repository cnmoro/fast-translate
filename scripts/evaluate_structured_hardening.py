#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import Any, Iterable

from datasets import load_dataset

from fast_translate import Translator
from fast_translate.structure import extract_code_segments, extract_latex_segments

EN_STRUCTURED_TEMPLATES = [
    "Technical snippet:\n```python\nfor i in range(3):\n    print(i)\n```\nExplain without changing the code.",
    "Consider the formula: $$E = mc^2$$ and describe it in plain language.",
    "Keep `pip install fast-translate` unchanged and translate only normal text.",
    "Math block:\n\\begin{equation}\n\\int_0^1 x^2 dx = 1/3\n\\end{equation}\nContinue with natural language.",
]

PT_STRUCTURED_TEMPLATES = [
    "Trecho técnico:\n```python\nfor i in range(3):\n    print(i)\n```\nExplique sem alterar o código.",
    "Considere a fórmula: $$E = mc^2$$ e descreva em linguagem simples.",
    "Mantenha `pip install fast-translate` igual e traduza apenas o restante.",
    "Bloco matemático:\n\\begin{equation}\n\\int_0^1 x^2 dx = 1/3\n\\end{equation}\nContinue com texto natural.",
]


def extract_code_spans(text: str) -> list[str]:
    return extract_code_segments(text)


def extract_latex_spans(text: str) -> list[str]:
    return extract_latex_segments(text)


def has_structured(text: str) -> bool:
    return bool(extract_code_spans(text) or extract_latex_spans(text))


def _coalesce_text_parts(parts: list[str]) -> str:
    clean = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    return "\n\n".join(clean)


def _ensure_structured_text(text: str, templates: list[str], idx: int) -> str:
    if has_structured(text):
        return text
    return f"{text}\n\n{templates[idx % len(templates)]}" if text.strip() else templates[idx % len(templates)]


def _safe_stream_rows(dataset: str, *, name: str | None = None, split: str = "train"):
    kwargs: dict[str, Any] = {"split": split, "streaming": True}
    if name:
        kwargs["name"] = name
    return load_dataset(dataset, **kwargs)


def _take_rows(dataset: str, extractor, *, limit: int, name: str | None = None, split: str = "train") -> tuple[list[str], str | None]:
    out: list[str] = []
    try:
        ds = _safe_stream_rows(dataset, name=name, split=split)
        for idx, row in enumerate(ds):
            text = extractor(row, idx)
            if not text:
                continue
            out.append(text)
            if len(out) >= limit:
                break
        return out, None
    except Exception as exc:
        return [], f"{dataset}({name or '-'}:{split}) -> {type(exc).__name__}: {exc}"


def gather_ultrachat_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        messages = row.get("messages") or []
        chunks: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = str(msg.get("content", "")).strip()
            if content:
                chunks.append(content)
        return _ensure_structured_text(_coalesce_text_parts(chunks), EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("HuggingFaceH4/ultrachat_200k", extractor, limit=limit, split="train_sft")


def gather_numina_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        text = _coalesce_text_parts([str(row.get("problem", "")), str(row.get("solution", ""))])
        return _ensure_structured_text(text, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("AI-MO/NuminaMath-CoT", extractor, limit=limit, split="train")


def gather_codesearchnet_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        doc = str(row.get("func_documentation_string", "")).strip()
        code = str(row.get("func_code_string", "")).strip()
        if not doc and not code:
            return ""
        if code:
            code = code[:800]
            return f"{doc}\n\n```python\n{code}\n```".strip()
        return _ensure_structured_text(doc, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("code_search_net", extractor, limit=limit, name="python", split="train")


def gather_lambda_hermes_samples(limit: int, config: str) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        task = str(row.get("task", ""))
        conversations = row.get("conversations") or []
        conv_lines: list[str] = []
        for c in conversations[:8]:
            if isinstance(c, dict):
                role = str(c.get("from", ""))
                val = str(c.get("value", ""))
                if val.strip():
                    conv_lines.append(f"[{role}] {val}")
        tools = str(row.get("tools", ""))
        merged = _coalesce_text_parts([task, "\n".join(conv_lines), tools])
        return _ensure_structured_text(merged, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("lambda/hermes-agent-reasoning-traces", extractor, limit=limit, name=config, split="train")


def gather_crownelius_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        merged = _coalesce_text_parts([
            str(row.get("problem", "")),
            str(row.get("thinking", "")),
            str(row.get("solution", "")),
        ])
        return _ensure_structured_text(merged, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("Crownelius/Opus-4.6-Reasoning-3300x", extractor, limit=limit, split="train")


def gather_swebench_pro_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        problem = str(row.get("problem_statement", "")).strip()
        patch = str(row.get("patch", "")).strip()[:2400]
        test_patch = str(row.get("test_patch", "")).strip()[:1600]
        merged = _coalesce_text_parts([
            problem,
            f"```diff\n{patch}\n```" if patch else "",
            f"```diff\n{test_patch}\n```" if test_patch else "",
        ])
        return _ensure_structured_text(merged, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("ScaleAI/SWE-bench_Pro", extractor, limit=limit, split="test")


def gather_gsm8k_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        q = str(row.get("question", ""))
        a = str(row.get("answer", ""))
        merged = _coalesce_text_parts([q, a])
        return _ensure_structured_text(merged, EN_STRUCTURED_TEMPLATES, idx)

    return _take_rows("openai/gsm8k", extractor, limit=limit, name="main", split="train")


def gather_pt_samples(limit: int) -> tuple[list[str], str | None]:
    def extractor(row: dict[str, Any], idx: int) -> str:
        base = str(row.get("output", "")).strip()
        return _ensure_structured_text(base, PT_STRUCTURED_TEMPLATES, idx)

    return _take_rows("orion-research/little-stories-en_US-pt_BR", extractor, limit=limit, split="train")


def project_to_pt_context(en_text: str, idx: int) -> str:
    # Use requested EN datasets to create PT->EN test prompts with shared structured content.
    return (
        "Analise o conteúdo abaixo e mantenha blocos técnicos sem alterações.\n\n"
        f"{en_text}\n\n"
        f"{PT_STRUCTURED_TEMPLATES[idx % len(PT_STRUCTURED_TEMPLATES)]}"
    )


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

    en_collectors = [
        ("HuggingFaceH4/ultrachat_200k(train_sft)", gather_ultrachat_samples),
        ("AI-MO/NuminaMath-CoT(train)", gather_numina_samples),
        ("code_search_net/python(train)", gather_codesearchnet_samples),
        ("lambda/hermes-agent-reasoning-traces(kimi/train)", lambda n: gather_lambda_hermes_samples(n, "kimi")),
        ("lambda/hermes-agent-reasoning-traces(glm-5.1/train)", lambda n: gather_lambda_hermes_samples(n, "glm-5.1")),
        ("Crownelius/Opus-4.6-Reasoning-3300x(train)", gather_crownelius_samples),
        ("ScaleAI/SWE-bench_Pro(test)", gather_swebench_pro_samples),
        ("openai/gsm8k(main/train)", gather_gsm8k_samples),
    ]

    per_collector = max(12, args.limit_en // len(en_collectors))
    en_texts: list[str] = []
    warnings: list[str] = []
    dataset_counts: dict[str, int] = {}

    for name, fn in en_collectors:
        texts, err = fn(per_collector)
        if err:
            warnings.append(err)
        dataset_counts[name] = len(texts)
        en_texts.extend(texts)

    random.shuffle(en_texts)
    en_texts = en_texts[: args.limit_en]

    # PT sources: native PT-BR set + PT projection from requested EN datasets.
    pt_native, pt_err = gather_pt_samples(max(20, args.limit_pt // 2))
    if pt_err:
        warnings.append(pt_err)

    projected = [project_to_pt_context(t, i) for i, t in enumerate(en_texts[: max(20, args.limit_pt // 2)])]
    pt_texts = pt_native + projected
    random.shuffle(pt_texts)
    pt_texts = pt_texts[: args.limit_pt]

    with Translator() as tr:
        en_metrics = evaluate_direction(tr, en_texts, "en-pt")
        pt_metrics = evaluate_direction(tr, pt_texts, "pt-en")

    report = {
        "datasets_requested": [
            "lambda/hermes-agent-reasoning-traces",
            "Crownelius/Opus-4.6-Reasoning-3300x",
            "ScaleAI/SWE-bench_Pro",
            "openai/gsm8k",
        ],
        "dataset_counts_en": dataset_counts,
        "limits": {"en": len(en_texts), "pt": len(pt_texts)},
        "metrics": {"en_pt": en_metrics, "pt_en": pt_metrics},
        "target_combined_exact_rate": 0.99,
        "warnings": warnings,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
