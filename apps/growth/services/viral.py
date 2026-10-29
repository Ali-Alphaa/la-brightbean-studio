"""The "this post went viral for its author" rule, and the author baseline it needs.

Pure functions over plain lists so the rule is easy to test and identical for
every source (browser extension, Business Discovery, save by link).

A post is viral when it has at least ``MIN_LIKES`` likes, its author has at
least ``MIN_OTHER_SAMPLES`` *other* known posts, and its likes are at least
``MULTIPLIER`` times the median of those posts. With fewer samples the post
can't be judged yet: it is held as *pending* and re-checked as more of the
author's posts are seen.
"""

from __future__ import annotations

from statistics import median

MIN_LIKES = 50
MIN_OTHER_SAMPLES = 3
MULTIPLIER = 3.0
#: Samples kept per author (newest last).
MAX_SAMPLES = 30
#: Undecided candidates kept per author.
MAX_PENDING = 10

VIRAL = "viral"
NOT_VIRAL = "not_viral"
PENDING = "pending"


def _others(samples: list[dict], post_id: str) -> list[int]:
    return [int(s["likes"]) for s in samples if str(s.get("id")) != str(post_id) and s.get("likes") is not None]


def ratio(likes: int | None, samples: list[dict], post_id: str) -> float | None:
    """``likes`` ÷ the median of the author's other posts, or None when unknown."""
    others = _others(samples, post_id)
    if likes is None or not others:
        return None
    return round(likes / max(median(others), 1), 2)


def decide(likes: int | None, samples: list[dict], post_id: str) -> str:
    """Classify one post against its author's other samples."""
    if likes is None or likes < MIN_LIKES:
        return NOT_VIRAL
    others = _others(samples, post_id)
    if len(others) < MIN_OTHER_SAMPLES:
        return PENDING
    return VIRAL if likes >= MULTIPLIER * max(median(others), 0) else NOT_VIRAL


def add_sample(samples: list[dict], post_id: str, likes: int | None) -> list[dict]:
    """Record (or refresh) one post's likes; keep the newest ``MAX_SAMPLES`` distinct posts."""
    if likes is None or not post_id:
        return list(samples)
    kept = [s for s in samples if str(s.get("id")) != str(post_id)]
    kept.append({"id": str(post_id), "likes": int(likes)})
    return kept[-MAX_SAMPLES:]


def add_pending(pending: list[dict], item: dict) -> list[dict]:
    """Hold an undecided item (``item["id"]`` identifies it); keep the newest ``MAX_PENDING``."""
    kept = [p for p in pending if str(p.get("id")) != str(item.get("id"))]
    kept.append(item)
    return kept[-MAX_PENDING:]


def resolve_pending(pending: list[dict], samples: list[dict]) -> tuple[list[dict], list[dict]]:
    """Re-check held items now that there may be more samples.

    Returns ``(now_viral, still_pending)``; items that turned out not viral are dropped.
    """
    viral, waiting = [], []
    for item in pending:
        verdict = decide(item.get("likes"), samples, str(item.get("id")))
        if verdict == VIRAL:
            viral.append(item)
        elif verdict == PENDING:
            waiting.append(item)
    return viral, waiting
