"""Deterministic Markdown heading chunking shared by production ingest and spikes."""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class MarkdownChunk:
    ordinal: int
    heading_path: str
    start_offset: int
    end_offset: int
    content: str


def markdown_heading_chunks(markdown: str) -> list[MarkdownChunk]:
    """Split Markdown at headings while retaining deterministic heading paths."""
    matches = list(_HEADING.finditer(markdown))
    if not matches:
        content = markdown.strip()
        return [MarkdownChunk(0, "", 0, len(markdown), content)] if content else []

    chunks: list[MarkdownChunk] = []
    headings: list[str] = []
    for ordinal, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        headings = headings[: level - 1]
        headings.append(title)
        start = match.start()
        end = matches[ordinal + 1].start() if ordinal + 1 < len(matches) else len(markdown)
        content = markdown[start:end].strip()
        if content:
            chunks.append(
                MarkdownChunk(
                    ordinal=len(chunks),
                    heading_path=" > ".join(headings),
                    start_offset=start,
                    end_offset=end,
                    content=content,
                )
            )
    return chunks
