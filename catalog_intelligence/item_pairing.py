"""Shared helper for recognizing companion SKUs (left/right piece pairs sold
together as one install, e.g. a stair-nose or herringbone/parquet piece set)
so they don't get flagged as catalog redundancy -- they're supposed to be
near-identical and are supposed to move together."""

from __future__ import annotations


def strip_side_suffix(item: str) -> str:
    item = str(item).strip().upper()
    for suffix in ("-L", "-R"):
        if item.endswith(suffix):
            return item[: -len(suffix)]
    return item


def is_companion_pair(a: str, b: str) -> bool:
    return strip_side_suffix(a) == strip_side_suffix(b) and a != b
