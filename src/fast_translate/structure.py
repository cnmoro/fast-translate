from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class ProtectedSpan:
    start: int
    end: int


_FENCED_CODE_RE = re.compile(r"(^|\n)(```|~~~)[^\n]*\n.*?\n\2(?=\n|$)", flags=re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_LATEX_TRIPLE_RE = re.compile(r"\$\$\$.*?\$\$\$", flags=re.DOTALL)
_LATEX_DISPLAY_RE = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)", flags=re.DOTALL)
_LATEX_ENV_RE = re.compile(
    r"\\begin\{(equation\*?|align\*?|alignat\*?|gather\*?|multline\*?|eqnarray\*?|math|displaymath|verbatim|lstlisting|minted)\}.*?\\end\{\1\}",
    flags=re.DOTALL,
)
_PLACEHOLDER_RE = re.compile(r"__FAST_TRANSLATE_KEEP_(\d+)__")


def maybe_has_structured_content(text: str) -> bool:
    if "```" in text or "~~~" in text or "`" in text:
        return True
    if "\\begin{" in text or "\\[" in text or "\\(" in text or "$$" in text:
        return True
    if "$" in text:
        # Fast path: if we can find a balanced non-escaped single-dollar pair,
        # run the structured-preservation pipeline.
        non_escaped = re.findall(r"(?<!\\)\$", text)
        if len(non_escaped) >= 2:
            return True
    return False


def extract_code_segments(text: str) -> list[str]:
    spans: list[ProtectedSpan] = []
    _add_spans(spans, text, _FENCED_CODE_RE)
    _add_spans(spans, text, _INLINE_CODE_RE)
    spans = _normalize_spans(spans)
    return [text[s.start : s.end] for s in spans]


def extract_latex_segments(text: str) -> list[str]:
    spans: list[ProtectedSpan] = []
    _add_spans(spans, text, _LATEX_TRIPLE_RE)
    _add_spans(spans, text, _LATEX_ENV_RE)
    _add_spans(spans, text, _LATEX_DISPLAY_RE)
    _add_inline_math_spans(spans, text, protect_all=False)
    # For evaluation/reporting, do not merge overlapping spans: a malformed
    # broad match should not hide a valid narrow LaTeX block.
    spans = sorted(spans, key=lambda s: (s.start, s.end))
    dedup: list[ProtectedSpan] = []
    seen = set()
    for s in spans:
        key = (s.start, s.end)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(s)
    segments = [text[s.start : s.end] for s in dedup]
    return [seg for seg in segments if _count_as_latex_segment(seg)]


def translate_preserving_structure(text: str, translate_line: Callable[[str], str]) -> str:
    spans = _collect_protected_spans(text)
    if not spans:
        return _translate_text_preserving_layout(text, translate_line)

    marked, originals = _inject_placeholders(text, spans)
    pieces = re.split(r"(__FAST_TRANSLATE_KEEP_\d+__)", marked)
    out: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        m = _PLACEHOLDER_RE.fullmatch(piece)
        if m:
            idx = int(m.group(1))
            out.append(originals[idx])
            continue
        out.append(_translate_text_preserving_layout(piece, translate_line))
    return "".join(out)


def _translate_text_preserving_layout(text: str, translate_line: Callable[[str], str]) -> str:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    out: list[str] = []
    for line in lines:
        core = line.rstrip("\r\n")
        ending = line[len(core) :]
        if not core.strip():
            out.append(line)
            continue
        leading_len = len(core) - len(core.lstrip())
        trailing_len = len(core) - len(core.rstrip())
        leading = core[:leading_len]
        trailing = core[len(core) - trailing_len :] if trailing_len else ""
        middle = core[leading_len : len(core) - trailing_len if trailing_len else len(core)]
        translated = translate_line(middle) if middle.strip() else middle
        out.append(f"{leading}{translated}{trailing}{ending}")
    return "".join(out)


def _collect_protected_spans(text: str) -> list[ProtectedSpan]:
    spans: list[ProtectedSpan] = []
    _add_spans(spans, text, _LATEX_TRIPLE_RE)
    _add_spans(spans, text, _FENCED_CODE_RE)
    _add_spans(spans, text, _LATEX_ENV_RE)
    _add_spans(spans, text, _LATEX_DISPLAY_RE)
    _add_inline_math_spans(spans, text, protect_all=True)
    _add_spans(spans, text, _INLINE_CODE_RE)
    return _normalize_spans(spans)


def _add_spans(spans: list[ProtectedSpan], text: str, pattern: re.Pattern[str]) -> None:
    for match in pattern.finditer(text):
        spans.append(ProtectedSpan(match.start(), match.end()))


def _add_inline_math_spans(spans: list[ProtectedSpan], text: str, *, protect_all: bool) -> None:
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "$" or (i > 0 and text[i - 1] == "\\"):
            i += 1
            continue
        if i + 1 < n and text[i + 1] == "$":
            i += 2
            continue
        j = i + 1
        while j < n:
            if text[j] == "$" and text[j - 1] != "\\":
                content = text[i + 1 : j]
                if protect_all:
                    # Preserve any balanced single-dollar fragment to avoid
                    # accidental corruption of technical/currency notations.
                    if content.strip() and len(content) <= 220:
                        spans.append(ProtectedSpan(i, j + 1))
                elif _looks_like_math(content):
                    spans.append(ProtectedSpan(i, j + 1))
                i = j + 1
                break
            if text[j] == "\n":
                i += 1
                break
            j += 1
        else:
            i += 1


def _looks_like_math(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    # Common inline math like $m$, $n$, $x_i$ should be treated as math.
    if re.fullmatch(r"[A-Za-z](?:[A-Za-z0-9_]{0,7})", stripped):
        return True

    if len(stripped) > 96:
        return False

    math_markers = ("\\", "^", "_", "=", "/", "*", "{", "}", "(", ")", "<", ">", "|")
    has_marker = any(marker in stripped for marker in math_markers)
    if not has_marker:
        return False

    # Avoid classifying natural-language currency/text fragments as math.
    words = re.findall(r"[A-Za-z]+", stripped)
    if words:
        allowed_words = {
            "frac",
            "sqrt",
            "sum",
            "prod",
            "lim",
            "mod",
            "sin",
            "cos",
            "tan",
            "log",
            "ln",
            "max",
            "min",
            "boxed",
            "binom",
            "text",
            "overrightarrow",
        }
        if any((len(w) != 1) and (w.lower() not in allowed_words) for w in words):
            return False

    # Regex/URL-like or command blobs are not math.
    if any(tok in stripped for tok in ("http://", "https://", "@", "\\x", "://")):
        return False

    return True


def _count_as_latex_segment(segment: str) -> bool:
    s = segment.strip()
    if not s:
        return False
    if s.startswith("\\begin{") or s.startswith("\\[") or s.startswith("\\("):
        return True
    if s.startswith("$$$") and s.endswith("$$$") and len(s) >= 6:
        inner = s[3:-3].strip()
        return _looks_like_math(inner)
    if s.startswith("$$") and s.endswith("$$") and len(s) >= 4:
        inner = s[2:-2].strip()
        return _looks_like_math(inner)
    if s.startswith("$") and s.endswith("$") and len(s) >= 2:
        inner = s[1:-1].strip()
        return _looks_like_math(inner)
    return False


def _normalize_spans(spans: list[ProtectedSpan]) -> list[ProtectedSpan]:
    if not spans:
        return []
    spans = sorted(spans, key=lambda s: (s.start, s.end))
    normalized: list[ProtectedSpan] = [spans[0]]
    for current in spans[1:]:
        last = normalized[-1]
        if current.start >= last.end:
            normalized.append(current)
            continue
        if current.end > last.end:
            normalized[-1] = ProtectedSpan(last.start, current.end)
    return normalized


def _inject_placeholders(text: str, spans: list[ProtectedSpan]) -> tuple[str, list[str]]:
    parts: list[str] = []
    originals: list[str] = []
    cursor = 0
    for idx, span in enumerate(spans):
        if span.start > cursor:
            parts.append(text[cursor : span.start])
        originals.append(text[span.start : span.end])
        parts.append(f"__FAST_TRANSLATE_KEEP_{idx}__")
        cursor = span.end
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts), originals
