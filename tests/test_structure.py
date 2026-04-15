from __future__ import annotations

from fast_translate.structure import maybe_has_structured_content, translate_preserving_structure


def _fake_translate(line: str) -> str:
    return f"<<{line.upper()}>>"


def test_preserves_fenced_code_block() -> None:
    src = "Explain this:\n```python\nfor i in range(3):\n    print(i)\n```\nThen summarize."
    out = translate_preserving_structure(src, _fake_translate)
    assert "```python\nfor i in range(3):\n    print(i)\n```" in out
    assert "<<EXPLAIN THIS:>>" in out
    assert "<<THEN SUMMARIZE.>>" in out


def test_preserves_inline_code_and_math() -> None:
    src = "Use `pip install fast-translate` and solve $x^2 + y^2 = z^2$ today."
    out = translate_preserving_structure(src, _fake_translate)
    assert "`pip install fast-translate`" in out
    assert "$x^2 + y^2 = z^2$" in out
    assert "<<USE>>" in out


def test_preserves_latex_environment() -> None:
    src = "Translate this sentence.\n\\begin{equation}\nE = mc^2\n\\end{equation}\nFinal note."
    out = translate_preserving_structure(src, _fake_translate)
    assert "\\begin{equation}\nE = mc^2\n\\end{equation}" in out
    assert "<<TRANSLATE THIS SENTENCE.>>" in out
    assert "<<FINAL NOTE.>>" in out


def test_detects_structured_content() -> None:
    assert maybe_has_structured_content("Text with `code`.")
    assert maybe_has_structured_content("Math: $$a+b$$")
    assert not maybe_has_structured_content("Plain sentence only.")
