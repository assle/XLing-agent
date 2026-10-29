"""Bounded content-keyed BM25 cache; score only postings touched by the query."""

from __future__ import annotations

import heapq
import re
from collections import Counter, defaultdict
from functools import lru_cache

from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]", text.lower())
    # Preserve the existing lexical feature contract for a controlled index change.
    compact = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    return words + [compact[i:i + 2] for i in range(len(compact) - 1)]


class LexicalIndex:
    def __init__(self, documents: tuple[str, ...]):
        tokens = [tokenize(document) for document in documents]
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        if not any(tokens):
            return
        model = BM25Okapi(tokens)
        for i, frequencies in enumerate(model.doc_freqs):
            norm = model.k1 * (1 - model.b + model.b * model.doc_len[i] / model.avgdl)
            for term, tf in frequencies.items():
                weight = model.idf[term] * tf * (model.k1 + 1) / (tf + norm)
                self.postings[term].append((i, weight))

    def scores(self, query: str) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for term, frequency in Counter(tokenize(query)).items():
            for i, weight in self.postings.get(term, ()):
                scores[i] += frequency * weight
        return scores

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        return heapq.nsmallest(top_k, ((i, score) for i, score in self.scores(query).items() if score > 0),
                               key=lambda item: (-item[1], item[0]))


@lru_cache(maxsize=4)
def lexical_index(documents: tuple[str, ...]) -> LexicalIndex:
    """Content, order and cardinality are the key; edits cannot reuse stale weights."""
    return LexicalIndex(documents)
