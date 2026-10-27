"""Performance analysis over the analytics snapshots: post scores, best times, hashtags.

Every post is scored against its own account, never across accounts: 300 likes
is a great day for one channel and a bad one for another. A post's ``lift`` is
its score divided by the median score of its account, so 1.0 is a typical post
and 2.0 did twice as well. Buckets (time slots, hashtags) average the lifts of
their posts, shrunk towards 1.0 so that one lucky post can't make a slot or a
tag look like a sure thing.
"""

from __future__ import annotations

import re
import statistics
import zoneinfo
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Any

from django.utils import timezone

from apps.analytics.metrics import ENGAGEMENT_DENOMINATORS, ENGAGEMENT_PARTS, post_metrics_for
from apps.analytics.services import _latest_post_stats
from apps.composer.models import PlatformPost

#: Weight of the "this is a typical post" prior when averaging bucket lifts.
PRIOR_WEIGHT = 2.0
#: Below this many scored posts the account's own history is too thin to trust.
MIN_POSTS_FOR_BEST_TIMES = 10
#: Share of posts that must report a reach-style denominator to score by rate.
RATE_BASIS_MIN_SHARE = 0.7
#: Default analysis window.
DEFAULT_LOOKBACK_DAYS = 180

#: Used until an account has enough history. Weekday mid-morning, lunch and
#: early evening are the slots most published studies agree on across networks.
DEFAULT_BEST_SLOTS: list[tuple[int, int]] = [
    (1, 9),
    (2, 12),
    (3, 9),
    (1, 17),
    (3, 17),
    (0, 12),
    (4, 9),
]

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

HASHTAG_RE = re.compile(r"(?<![\w#])#([^\W\d_][\w]{0,99})", re.UNICODE)
WORD_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


@dataclass
class ScoredPost:
    platform_post: PlatformPost
    stats: dict[str, float]
    engagements: float
    score: float
    basis: str  # "rate" (engagements per 100 views/reach/impressions) or "count"
    lift: float = 1.0

    @property
    def caption(self) -> str:
        pp = self.platform_post
        return (pp.platform_specific_caption or pp.post.caption or "").strip()


@dataclass
class Slot:
    weekday: int
    hour: int
    posts: int
    lift: float

    @property
    def label(self) -> str:
        return f"{DAY_NAMES[self.weekday]} {self.hour:02d}:00"


@dataclass
class BestTimes:
    source: str  # "history" or "default"
    scored_posts: int
    top: list[Slot]
    # grid[weekday][hour] -> (posts, smoothed lift) or None when no post landed there
    grid: list[list[tuple[int, float] | None]] = field(default_factory=list)


@dataclass
class HashtagStat:
    tag: str
    uses: int
    lift: float


def engagement_total(stats: dict[str, float]) -> float:
    return float(sum(stats.get(k, 0.0) or 0.0 for k in ENGAGEMENT_PARTS))


def _denominator(stats: dict[str, float]) -> float:
    for key in ENGAGEMENT_DENOMINATORS:
        value = stats.get(key) or 0.0
        if value > 0:
            return float(value)
    return 0.0


def smoothed_lift(lifts: list[float]) -> float:
    return (sum(lifts) + PRIOR_WEIGHT * 1.0) / (len(lifts) + PRIOR_WEIGHT)


def published_platform_posts(accounts: Iterable[Any], *, days: int | None = DEFAULT_LOOKBACK_DAYS):
    qs = PlatformPost.objects.filter(
        social_account__in=list(accounts),
        status=PlatformPost.Status.PUBLISHED,
        published_at__isnull=False,
    ).select_related("post", "social_account")
    if days is not None:
        qs = qs.filter(published_at__gte=timezone.now() - timedelta(days=days))
    return qs.order_by("-published_at")


def score_posts(platform_posts: Iterable[PlatformPost]) -> list[ScoredPost]:
    """Score posts with analytics against the other posts of the same account.

    Posts with no snapshot yet are skipped: an unmeasured post is not a bad post.
    """
    by_account: dict[Any, list[PlatformPost]] = defaultdict(list)
    for pp in platform_posts:
        by_account[pp.social_account_id].append(pp)

    scored: list[ScoredPost] = []
    for posts in by_account.values():
        platform = posts[0].social_account.platform
        stats_by_post = _latest_post_stats([p.id for p in posts], post_metrics_for(platform))
        measured = [(p, stats_by_post[p.id]) for p in posts if stats_by_post.get(p.id)]
        if not measured:
            continue

        with_denominator = sum(1 for _, s in measured if _denominator(s) > 0)
        basis = "rate" if with_denominator / len(measured) >= RATE_BASIS_MIN_SHARE else "count"

        account_scored: list[ScoredPost] = []
        for pp, stats in measured:
            engagements = engagement_total(stats)
            if basis == "rate":
                denom = _denominator(stats)
                if denom <= 0:
                    continue
                score = engagements / denom * 100
            else:
                score = engagements
            account_scored.append(ScoredPost(pp, stats, engagements, score, basis))

        baseline = statistics.median(s.score for s in account_scored) if account_scored else 0.0
        if baseline <= 0:
            mean = statistics.fmean(s.score for s in account_scored) if account_scored else 0.0
            baseline = mean
        for s in account_scored:
            s.lift = (s.score / baseline) if baseline > 0 else 1.0
        scored.extend(account_scored)
    return scored


def best_times(scored: list[ScoredPost], tz_name: str, *, top_n: int = 7) -> BestTimes:
    tz = zoneinfo.ZoneInfo(tz_name or "UTC")
    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for s in scored:
        local = s.platform_post.published_at.astimezone(tz)
        buckets[(local.weekday(), local.hour)].append(s.lift)

    grid: list[list[tuple[int, float] | None]] = [[None] * 24 for _ in range(7)]
    for (day, hour), lifts in buckets.items():
        grid[day][hour] = (len(lifts), round(smoothed_lift(lifts), 2))

    if len(scored) < MIN_POSTS_FOR_BEST_TIMES:
        top = [Slot(d, h, 0, 1.0) for d, h in DEFAULT_BEST_SLOTS[:top_n]]
        return BestTimes(source="default", scored_posts=len(scored), top=top, grid=grid)

    ranked = sorted(
        (Slot(day, hour, len(lifts), round(smoothed_lift(lifts), 2)) for (day, hour), lifts in buckets.items()),
        key=lambda slot: (slot.lift, slot.posts),
        reverse=True,
    )
    # Only recommend slots that beat a typical post; pad with defaults otherwise.
    top = [slot for slot in ranked if slot.lift > 1.0][:top_n]
    taken = {(slot.weekday, slot.hour) for slot in top}
    for day, hour in DEFAULT_BEST_SLOTS:
        if len(top) >= top_n:
            break
        if (day, hour) not in taken:
            top.append(Slot(day, hour, 0, 1.0))
    return BestTimes(source="history", scored_posts=len(scored), top=top, grid=grid)


def apply_best_times(social_account, slots: list[Slot], *, replace: bool = False) -> int:
    """Turn recommended slots into the account's posting slots. Returns slots added."""
    from apps.calendar.models import PostingSlot

    if replace:
        PostingSlot.objects.filter(social_account=social_account).update(is_active=False)
    added = 0
    for slot in slots:
        obj, created = PostingSlot.objects.get_or_create(
            social_account=social_account,
            day_of_week=slot.weekday,
            time=time(slot.hour, 0),
            defaults={"is_active": True},
        )
        if created:
            added += 1
        elif not obj.is_active:
            obj.is_active = True
            obj.save(update_fields=["is_active", "updated_at"])
            added += 1
    return added


def extract_hashtags(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in HASHTAG_RE.findall(text or ""):
        seen.setdefault(match.lower(), None)
    return list(seen)


def hashtag_stats(scored: list[ScoredPost], *, min_uses: int = 1) -> list[HashtagStat]:
    lifts: dict[str, list[float]] = defaultdict(list)
    for s in scored:
        for tag in extract_hashtags(s.caption):
            lifts[tag].append(s.lift)
    stats = [
        HashtagStat(tag=tag, uses=len(values), lift=round(smoothed_lift(values), 2))
        for tag, values in lifts.items()
        if len(values) >= min_uses
    ]
    stats.sort(key=lambda h: (h.lift, h.uses), reverse=True)
    return stats


def _words(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(HASHTAG_RE.sub(" ", text or ""))}


def suggest_hashtags_from_history(caption: str, scored: list[ScoredPost], *, limit: int = 10) -> list[HashtagStat]:
    """Hashtags that did well before, preferring those used on similar captions."""
    already = set(extract_hashtags(caption))
    caption_words = _words(caption)
    stats = {h.tag: h for h in hashtag_stats(scored)}
    relevance: dict[str, float] = defaultdict(float)
    for s in scored:
        tags = extract_hashtags(s.caption)
        if not tags:
            continue
        overlap = len(caption_words & _words(s.caption))
        for tag in tags:
            relevance[tag] += overlap

    def rank(h: HashtagStat) -> float:
        return h.lift * (1.0 + min(relevance.get(h.tag, 0.0), 10.0) / 5.0)

    # Only tags that beat a typical post are worth recommending.
    candidates = [h for tag, h in stats.items() if tag not in already and h.lift > 1.0]
    candidates.sort(key=rank, reverse=True)
    return candidates[:limit]


def top_posts(scored: list[ScoredPost], *, top_percent: int = 20, limit: int | None = None) -> list[ScoredPost]:
    """Posts in the top ``top_percent`` of their own account, best first."""
    by_account: dict[Any, list[ScoredPost]] = defaultdict(list)
    for s in scored:
        by_account[s.platform_post.social_account_id].append(s)
    picked: list[ScoredPost] = []
    for posts in by_account.values():
        posts.sort(key=lambda s: s.score, reverse=True)
        keep = max(1, round(len(posts) * max(1, min(top_percent, 100)) / 100))
        picked.extend(p for p in posts[:keep] if p.lift >= 1.0)
    picked.sort(key=lambda s: s.lift, reverse=True)
    return picked[:limit] if limit else picked
