"""Word diff between the scan's word boxes and the transcription's words."""

from __future__ import annotations

from unscanner.diff import word_diff


def boxes(lines: list[str]) -> list[dict]:
    """Word boxes laid out one line of text per row, as pdf.cached_page_words returns them."""
    out = []
    for row, line in enumerate(lines):
        for col, text in enumerate(line.split()):
            out.append({"text": text, "x0": 50 + col * 60, "y0": 100 + row * 20, "x1": 105 + col * 60, "y1": 115 + row * 20})
    return out


SCAN = boxes(["The quick brown fox jumps over", "the lazy dog and runs far", "away into the dark forest."])


def texts(result: dict, side: str, kind: str, words: list[str] | None = None) -> list[str]:
    if side == "scan":
        return [t["text"] for t in result["scan"] if t["kind"] == kind]
    return [words[e["index"]] for e in result["editor"] if e["kind"] == kind]


def test_identical_text_has_no_differences():
    words = "THE QUICK, brown fox jumps over the lazy dog — and runs far away into the dark forest".split()
    r = word_diff(SCAN, words)
    assert r["scan"] == [] and r["editor"] == []
    assert r["counts"] == {"missing": 0, "extra": 0, "changed": 0}


def test_dropped_line_is_missing_on_the_scan_with_its_boxes():
    words = "The quick brown fox jumps over away into the dark forest.".split()
    r = word_diff(SCAN, words)
    assert texts(r, "scan", "missing") == ["the", "lazy", "dog", "and", "runs", "far"]
    assert r["editor"] == []
    assert r["scan"][0]["boxes"] == [{"x0": 50, "y0": 120, "x1": 105, "y1": 135}]
    assert r["counts"]["missing"] == 6


def test_invented_text_is_extra_in_the_editor():
    words = "The quick brown fox jumps over the lazy dog and then quietly runs far away into the dark forest.".split()
    r = word_diff(SCAN, words)
    assert texts(r, "editor", "extra", words) == ["then", "quietly"]
    assert r["scan"] == []


def test_near_match_is_changed_on_both_sides():
    words = "The quick brown fax jumps over the lazy dog and runs far away into the dark forest.".split()
    r = word_diff(SCAN, words)
    assert texts(r, "scan", "changed") == ["fox"] and texts(r, "editor", "changed", words) == ["fax"]
    assert r["counts"] == {"missing": 0, "extra": 0, "changed": 1}


def test_unrelated_replacement_is_missing_plus_extra():
    words = "The quick brown elephant jumps over the lazy dog and runs far away into the dark forest.".split()
    r = word_diff(SCAN, words)
    assert texts(r, "scan", "missing") == ["fox"] and texts(r, "editor", "extra", words) == ["elephant"]


def test_line_end_hyphenation_is_one_word_with_two_boxes():
    scan = boxes(["a word that is hyphen-", "ated here and well-known there"])
    assert word_diff(scan, "a word that is hyphenated here and well-known there".split())["counts"] == \
        {"missing": 0, "extra": 0, "changed": 0}
    r = word_diff(scan, "a word that is here and well-known there".split())
    assert texts(r, "scan", "missing") == ["hyphen-ated"]
    assert len(r["scan"][0]["boxes"]) == 2


def test_words_split_differently_are_equal():
    scan = boxes(["we went to gether to the foot note"])
    assert word_diff(scan, "we went together to the footnote".split())["editor"] == []


def test_moved_passage_is_not_reported():
    scan = boxes(["1 This footnote sits low on the page.", "Body text follows the note here."])
    words = "Body text follows the note here. 1 This footnote sits low on the page.".split()
    assert word_diff(scan, words)["counts"] == {"missing": 0, "extra": 0, "changed": 0}


def test_editor_indexes_count_punctuation_only_words():
    words = "The quick brown fox — jumps over the lazy dog and runs far away into the dark wet forest.".split()
    r = word_diff(SCAN, words)
    assert [words[e["index"]] for e in r["editor"]] == ["wet"]


def test_empty_sides():
    assert word_diff([], [])["counts"] == {"missing": 0, "extra": 0, "changed": 0}
    assert word_diff(SCAN, [])["counts"]["missing"] == 17
    assert word_diff([], ["only", "here"])["counts"]["extra"] == 2
