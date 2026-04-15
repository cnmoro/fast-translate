from __future__ import annotations

import re

from fast_translate.structure import translate_preserving_structure


def _mock_en_pt(line: str) -> str:
    return f"[PT]{line}"


def _mock_pt_en(line: str) -> str:
    return f"[EN]{line}"


def _assert_preserved_blocks(src: str, out: str) -> None:
    code_patterns = [
        r"```[\s\S]*?```",
        r"`[^`\n]+`",
    ]
    latex_patterns = [
        r"\$\$[\s\S]*?\$\$",
        r"\\\[[\s\S]*?\\\]",
        r"\\\([\s\S]*?\\\)",
        r"\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|eqnarray\*?|math|displaymath)\}[\s\S]*?\\end\{\1\}",
    ]
    for pat in code_patterns + latex_patterns:
        src_blocks = re.findall(pat, src)
        out_blocks = re.findall(pat, out)
        assert src_blocks == out_blocks


def test_en_pt_use_cases_with_code_and_latex() -> None:
    cases = [
        "Please explain the function below without changing code:\n```python\ndef add(a, b):\n    return a + b\n```",
        "Keep `pip install fast-translate` untouched and translate the sentence.",
        "Given $$\\frac{d}{dx}x^2 = 2x$$, provide an intuitive explanation in Portuguese.",
        "Translate only prose: \\begin{equation}\nE = mc^2\n\\end{equation}\nThis equation links mass and energy.",
    ]
    for src in cases:
        out = translate_preserving_structure(src, _mock_en_pt)
        _assert_preserved_blocks(src, out)
        assert "[PT]" in out


def test_pt_en_use_cases_with_code_and_latex() -> None:
    cases = [
        "Explique o trecho sem alterar:\n```javascript\nconst sum = (a, b) => a + b;\n```",
        "Mantenha `docker build -t fast-translate .` igual e traduza o restante.",
        "Considere \\(a^2 + b^2 = c^2\\) e descreva o significado geométrico.",
        "Traduza apenas o texto comum.\n\\begin{align}\nf(x) &= x^2 \\\\\ng(x) &= x^3\n\\end{align}\nDepois compare f e g.",
    ]
    for src in cases:
        out = translate_preserving_structure(src, _mock_pt_en)
        _assert_preserved_blocks(src, out)
        assert "[EN]" in out
