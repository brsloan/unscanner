"""Word-level diff between the words on the scan and the words of a page's transcription.

The scan side is the page's word boxes (`pdf.cached_page_words`: text layer, else OCR), so every
difference can be drawn on the scan; the transcription side is the list of words the editor shows.
The result names three kinds of difference:

- missing: on the scan but not in the transcription (a dropped line; also running heads and page
  numbers, which the guidelines leave out on purpose)
- extra:   in the transcription but not on the scan (invented text)
- changed: a word on each side that nearly match (an OCR misreading or a model "correction")

Words are compared without case, punctuation or accents of form (NFKC), a word hyphenated across a
line end counts as one word, and a passage that only moved (footnotes, columns) is not reported.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_NON_WORD = re.compile(r"[\W_]+")
_LINE_END_HYPHENS = ("-", "\u2010", "\u00ad", "\u00ac")  # hyphen, soft hyphen, not sign (OCR)
SIMILAR = 0.6        # character ratio ("fox"/"fax" is 0.67) at which two different words count as "changed", not missing + extra
MOVE_MIN = 4         # a missing run found among the extra words counts as moved when at least this long
PAIR_LIMIT = 2500    # largest replaced block (scan words x transcription words) that is paired word by word


def norm(token: str) -> str:
    return _NON_WORD.sub("", unicodedata.normalize("NFKC", token).casefold())


def _same_line(a: dict, b: dict) -> bool:
    return abs((a["y0"] + a["y1"]) - (b["y0"] + b["y1"])) / 2 <= (a["y1"] - a["y0"]) / 2


def _scan_tokens(words: list[dict]) -> list[dict]:
    """Scan words as {key, text, boxes}; a word split by a line-end hyphen becomes one token with two boxes."""
    tokens: list[dict] = []
    carry: dict | None = None
    for i, w in enumerate(words):
        box = {k: w[k] for k in ("x0", "y0", "x1", "y1")}
        if carry is not None:
            tok = {"text": carry["text"] + w["text"], "boxes": carry["boxes"] + [box]}
            carry = None
        else:
            tok = {"text": w["text"], "boxes": [box]}
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is not None and w["text"].endswith(_LINE_END_HYPHENS) and norm(w["text"]) and not _same_line(w, nxt):
            carry = tok
            continue
        tok["key"] = norm(tok["text"])
        if tok["key"]:
            tokens.append(tok)
    return tokens


def _similar(a: str, b: str) -> bool:
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 4 and long_.startswith(short):  # OCR damage at a word end, as in locate.py
        return True
    m = SequenceMatcher(None, a, b, autojunk=False)
    return m.quick_ratio() >= SIMILAR and m.ratio() >= SIMILAR


def _pair(a: list[str], b: list[str]) -> list[tuple[int, int]]:
    """In-order pairs (i, j) of similar words from a replaced block: longest common subsequence under _similar."""
    if not a or not b or len(a) * len(b) > PAIR_LIMIT:
        return []
    sim = [[_similar(x, y) for y in b] for x in a]
    best = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) - 1, -1, -1):
        for j in range(len(b) - 1, -1, -1):
            best[i][j] = best[i + 1][j + 1] + 1 if sim[i][j] else max(best[i + 1][j], best[i][j + 1])
    pairs, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        if sim[i][j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def word_diff(scan_words: list[dict], editor_words: list[str]) -> dict:
    """Diff the scan's word boxes against the transcription's words.

    Returns {"scan": [{"kind": "missing"|"changed", "text", "boxes": [{x0,y0,x1,y1}, ...]}],
             "editor": [{"kind": "extra"|"changed", "index"}],   # index into editor_words
             "counts": {"missing", "extra", "changed"}}
    """
    a = _scan_tokens(scan_words)
    b = [(i, k) for i, k in ((i, norm(t)) for i, t in enumerate(editor_words)) if k]
    ak, bk = [t["key"] for t in a], [k for _, k in b]
    missing: list[int] = []
    extra: list[int] = []
    changed: list[tuple[int, int]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, ak, bk, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and "".join(ak[i1:i2]) == "".join(bk[j1:j2]):
            continue  # the same letters split into words differently ("to gether")
        pairs = _pair(ak[i1:i2], bk[j1:j2]) if op == "replace" else []
        changed += [(i1 + i, j1 + j) for i, j in pairs]
        paired_a, paired_b = {i1 + i for i, _ in pairs}, {j1 + j for _, j in pairs}
        missing += [i for i in range(i1, i2) if i not in paired_a]
        extra += [j for j in range(j1, j2) if j not in paired_b]

    # Text that only moved (footnotes set after the body, columns read in another order) is on both lists.
    moved = SequenceMatcher(None, [ak[i] for i in missing], [bk[j] for j in extra], autojunk=False)
    gone_a: set[int] = set()
    gone_b: set[int] = set()
    for m in moved.get_matching_blocks():
        if m.size >= MOVE_MIN:
            gone_a.update(missing[m.a:m.a + m.size])
            gone_b.update(extra[m.b:m.b + m.size])
    missing = [i for i in missing if i not in gone_a]
    extra = [j for j in extra if j not in gone_b]

    scan = [(i, "missing") for i in missing] + [(i, "changed") for i, _ in changed]
    editor = [(j, "extra") for j in extra] + [(j, "changed") for _, j in changed]
    return {
        "scan": [{"kind": kind, "text": a[i]["text"], "boxes": a[i]["boxes"]} for i, kind in sorted(scan)],
        "editor": [{"kind": kind, "index": b[j][0]} for j, kind in sorted(editor)],
        "counts": {"missing": len(missing), "extra": len(extra), "changed": len(changed)},
    }
