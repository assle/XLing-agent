"""Heading-aware passage packing with sentence boundaries and bounded overlap."""

from __future__ import annotations

import re

import numpy as np


def sections(content: str) -> list[tuple[str, str]]:
    headings: list[tuple[int, str]] = []
    result: list[tuple[str, str]] = []
    lines: list[str] = []

    def flush() -> None:
        body = "\n".join(lines).strip()
        if body:
            result.append(("\n".join(title for _, title in headings), body))
        lines.clear()

    for line in (content or "").splitlines():
        heading = re.match(r"^(#{1,6})\s+\S", line)
        if heading:
            flush()
            level = len(heading[1])
            headings = [(n, text) for n, text in headings if n < level]
            headings.append((level, line.strip()))
        else:
            lines.append(line.rstrip())
    flush()
    if not result and headings:
        result.append(("", "\n".join(title for _, title in headings)))
    return result


def sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？!?])|(?<=\.)\s+|\n+", text) if part.strip()]


def chunk_text(content: str, size: int, overlap: int) -> list[str]:
    """Keep section paths, pack whole sentences, and overlap only within a section.

    Oversize sentences are cut only as a last resort. Every emitted chunk fits size;
    overlap is an upper bound because a sentence is never cut just to fill it.
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Chunk size must be positive and overlap in [0, size)")
    chunks: list[str] = []
    for title, body in sections(content):
        # Very long headings must not leave no room for body text.
        prefix = title + "\n" if title and len(title) + 1 < size // 2 else ""
        if title and not prefix:
            body = title + "\n" + body
        capacity = size - len(prefix)
        units = [piece for sentence in sentences(body)
                 for piece in (sentence[i:i + capacity] for i in range(0, len(sentence), capacity))]
        current: list[str] = []
        for unit in units:
            if current and len("\n".join([*current, unit])) > capacity:
                chunks.append(prefix + "\n".join(current))
                tail: list[str] = []
                for previous in reversed(current):
                    if len("\n".join([previous, *tail])) > overlap:
                        break
                    tail.insert(0, previous)
                current = tail
                while current and len("\n".join([*current, unit])) > capacity:
                    current.pop(0)
            current.append(unit)
        if current:
            chunks.append(prefix + "\n".join(current))
    return chunks


def semantic_chunk_text(content: str, size: int, overlap: int, embed_texts) -> list[str]:
    """Split at adjacent sentence cosine-distance valleys within heading sections.

    Boundaries above the section's 75th distance percentile may split after 128
    body characters. Sentence packing enforces the same maximum size/overlap.
    Encoding happens only at ingestion, never on the query path.
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("Chunk size must be positive and overlap in [0, size)")
    parts = [(title, sentences(body)) for title, body in sections(content)]
    units = [unit for _, group in parts for unit in group]
    if not units:
        return []
    vectors = np.asarray(embed_texts(units), dtype=np.float32)
    if vectors.ndim != 2 or len(vectors) != len(units) or not np.isfinite(vectors).all():
        raise ValueError("Sentence embeddings must be finite and match sentence count")
    vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
    result: list[str] = []
    offset = 0
    for title, group in parts:
        local = vectors[offset:offset + len(group)]
        offset += len(group)
        distances = 1 - np.sum(local[:-1] * local[1:], axis=1)
        threshold = float(np.quantile(distances, .75)) if len(distances) else 1.0
        current: list[str] = []
        for i, unit in enumerate(group):
            length = len("\n".join(current))
            if current and (length + len(unit) + len(title) + 2 > size or
                            (length >= min(128, size // 2) and distances[i - 1] > threshold)):
                result.extend(chunk_text(title + "\n" + "\n".join(current), size, overlap))
                current = []
            current.append(unit)
        if current:
            result.extend(chunk_text(title + "\n" + "\n".join(current), size, overlap))
    return result
