from __future__ import annotations

"""
Backward-compatible enhanced postprocess module.

This module intentionally delegates to `fast_translate.postprocess` so there is
only one source of truth for post-editing rules inside `library/`.
"""

from .postprocess import post_edit_english, post_edit_portuguese, postprocess as _postprocess


def postprocess(text: str, direction: str, source_text: str | None = None) -> str:
    return _postprocess(text=text, direction=direction, source_text=source_text)

