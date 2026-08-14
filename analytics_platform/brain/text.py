"""Text preparation for the Brain's lexical recall leg.

FTS5 MATCH takes a query *expression*, not free text: `?`, `-`, `:`, `"` and the
bare words AND/OR/NOT are syntax and raise OperationalError. A user question is
therefore reduced to its content tokens, each quoted so it is treated as a
literal, joined with OR so any single overlap is a hit. Ranking is bm25()'s job,
not the query's.
"""
from __future__ import annotations

import re
from typing import List

# Deliberately small. An aggressive stoplist hurts a knowledge base full of short
# titles more than it helps; these are only the words that appear in nearly every
# analytics question and so carry no discriminating signal.
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "by", "from", "as",
    "and", "or", "not", "but", "if", "then", "than", "that", "this", "these",
    "those", "it", "its", "we", "our", "you", "your", "they", "their",
    "what", "which", "who", "whom", "when", "where", "why", "how",
    "do", "does", "did", "doing", "done", "can", "could", "should", "would",
    "have", "has", "had", "will", "shall", "may", "might", "must",
    "me", "my", "us", "i", "s", "t",
})

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> List[str]:
    """Content tokens, lowercased, order-preserving, de-duplicated."""
    seen = set()
    out: List[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        tok = raw.lower()
        if len(tok) < 2 or tok in STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def to_fts_query(text: str) -> str:
    """An FTS5 MATCH expression for `text`, or "" when nothing usable remains.

    Callers MUST treat "" as "skip the lexical leg" — passing it to MATCH is an
    OperationalError, not an empty result set.
    """
    tokens = tokenize(text)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
