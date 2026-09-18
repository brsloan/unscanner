"""Map a run of words from the editor to a word box on the scanned page.

The editor sends a short context (a few words before and after the caret) and the index of the
word the caret is on. We slide that context over the page's word list and pick the alignment with
the most matching tokens, then return the box of the page word aligned with the caret word.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(token: str) -> str:
    return _NON_ALNUM.sub("", token.lower())


def _match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    # Tolerate hyphenation at line ends and OCR damage at word ends: one is a prefix of the other.
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 4 and long_.startswith(short)


def locate(words: list[dict], context: list[str], index: int, min_score: int | None = None) -> dict | None:
    """Return {"x0","y0","x1","y1","score","text"} for the page word aligned with context[index], or None."""
    if not words or not context or not 0 <= index < len(context):
        return None
    q = [norm(t) for t in context]
    page = [norm(w["text"]) for w in words]
    need = min_score if min_score is not None else min(3, sum(1 for t in q if t))
    best_score, best_pos = 0, None
    for start in range(-index, len(page) - index):
        score = 0
        for j, t in enumerate(q):
            k = start + j
            if t and 0 <= k < len(page) and _match(t, page[k]):
                score += 1
        pos = start + index
        # Prefer alignments where the caret word itself matches; break ties by earlier position.
        bonus = 0.5 if 0 <= pos < len(page) and _match(q[index], page[pos]) else 0
        if score + bonus > best_score and 0 <= pos < len(page):
            best_score, best_pos = score + bonus, pos
    if best_pos is None or best_score < need:
        return None
    w = words[best_pos]
    return {"x0": w["x0"], "y0": w["y0"], "x1": w["x1"], "y1": w["y1"], "text": w["text"], "score": best_score}
