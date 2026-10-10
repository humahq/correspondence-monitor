"""Quote verification against original text.

PDF extraction inserts stray spaces ("mini -mental", "≥ 2 2"), so quotes are
compared after removing whitespace, unifying dashes and quotes, and lowercasing.
"""

from __future__ import annotations

import re
import unicodedata

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2212\u00ad"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"'}


def _norm_char_map(text: str) -> tuple[str, list[int]]:
    """Normalized text plus, for each normalized char, its index in the original."""
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        ch = unicodedata.normalize("NFKC", ch).translate(_DASHES).translate(_QUOTES)
        for c in ch:
            if c.isspace() or c in "•▪◦·":
                continue
            out.append(c.lower())
            idx.append(i)
    return "".join(out), idx


def normalize(text: str) -> str:
    return _norm_char_map(text)[0]


def _long_enough(nq: str, short_ok: bool) -> bool:
    if len(nq) >= 8:
        return True
    # site shorthand such as "GFR 41" or "bmi 18": a word and a number together are specific enough,
    # and so is a reading with several digits such as "162/96"
    if not short_ok or len(nq) < 4:
        return False
    digits = sum(c.isdigit() for c in nq)
    return (any(c.isalpha() for c in nq) and digits > 0) or (len(nq) >= 5 and digits >= 4)


def find_all_quotes(quote: str, text: str, short_ok: bool = False) -> list[tuple[int, int]]:
    """Every (start, end) occurrence of quote in the original text."""
    nq = normalize(quote)
    if not _long_enough(nq, short_ok):
        return []
    nt, idx = _norm_char_map(text)
    out, pos = [], nt.find(nq)
    while pos >= 0:
        out.append((idx[pos], idx[pos + len(nq) - 1] + 1))
        pos = nt.find(nq, pos + 1)
    return out


def find_quote(quote: str, text: str, short_ok: bool = False) -> tuple[int, int] | None:
    """Return (start, end) of the first occurrence of quote in the original text, or None."""
    found = find_all_quotes(quote, text, short_ok)
    return found[0] if found else None


def expand_to_sentence(quote: str, text: str, min_len: int = 40) -> str:
    """Widen a short quote to the sentence that contains it, so it reads in context."""
    span = find_quote(quote, text, short_ok=True)
    if not span or len(quote) >= min_len:
        return quote
    def boundary(i: int) -> bool:
        ch = text[i]
        if ch in "!?\n":
            return True
        return ch == "." and (i + 1 >= len(text) or text[i + 1].isspace())

    start, end = span
    while start > 0 and not boundary(start - 1):
        start -= 1
    while end < len(text) and not boundary(end):
        end += 1
    if end < len(text) and text[end] in ".!?":
        end += 1
    return clean_spaces(text[start:end])


def quote_in(quote: str, text: str, short_ok: bool = False) -> bool:
    return find_quote(quote, text, short_ok) is not None


def quote_pieces(quote: str, text: str) -> list[str]:
    """The quote, or the sentences it was stitched from, each found verbatim; in text order. Empty if any is missing."""
    if quote_in(quote, text, short_ok=True):
        return [quote]
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\s*(?:\.\.\.|…)\s*|\n+", quote)]
    parts = [p for p in parts if len(normalize(p)) >= 4]
    if len(parts) < 2:
        return []
    spans = [(find_quote(p, text, short_ok=True), p) for p in parts]
    if any(s is None for s, _ in spans):
        return []
    return [p for _, p in sorted(spans, key=lambda sp: sp[0][0])]


def passage_around(text: str, start: int, end: int, pad: int = 500) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return re.sub(r"[ \t]+", " ", text[s:e]).strip()


def clean_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
