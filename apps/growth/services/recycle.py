"""Evergreen recycling: put proven posts back on the calendar."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.composer.services import clone_post, transition_platform_post
from apps.social_accounts.models import SocialAccount

from . import ai
from .analysis import ScoredPost, published_platform_posts, score_posts, top_posts

logger = logging.getLogger(__name__)

#: History considered when picking posts to recycle.
RECYCLE_LOOKBACK_DAYS = 365
#: The automatic run happens at most once in this window per workspace.
RUN_EVERY = timedelta(hours=20)


def recycle_candidates(workspace, growth_settings) -> list[ScoredPost]:
    from apps.growth.models import RecycledPost

    accounts = SocialAccount.objects.filter(workspace=workspace).exclude(
        connection_status=SocialAccount.ConnectionStatus.DISCONNECTED
    )
    scored = score_posts(published_platform_posts(accounts, days=RECYCLE_LOOKBACK_DAYS))
    best = top_posts(scored, top_percent=growth_settings.recycle_top_percent)

    now = timezone.now()
    old_enough = now - timedelta(days=growth_settings.recycle_min_age_days)
    gap_start = now - timedelta(days=growth_settings.recycle_gap_days)
    recent = RecycledPost.objects.filter(workspace=workspace, created_at__gte=gap_start)
    blocked = set(recent.values_list("source_post_id", flat=True))
    # A recycled copy and its original are the same content: block both ways.
    blocked |= set(recent.exclude(new_post=None).values_list("new_post_id", flat=True))
    blocked |= set(
        RecycledPost.objects.filter(
            workspace=workspace, new_post__isnull=False, source_post_id__in=blocked
        ).values_list("new_post_id", flat=True)
    )
    copies = RecycledPost.objects.filter(workspace=workspace, new_post_id__isnull=False)
    original_of = dict(copies.values_list("new_post_id", "source_post_id"))

    picked: dict = {}
    for s in best:
        pp = s.platform_post
        if pp.published_at > old_enough:
            continue
        post_id = pp.post_id
        if post_id in blocked or original_of.get(post_id) in blocked:
            continue
        if post_id not in picked or s.lift > picked[post_id].lift:
            picked[post_id] = s
    return sorted(picked.values(), key=lambda s: s.lift, reverse=True)


def recycle_post(post, growth_settings, *, score: float = 0.0, author=None, automatic: bool = True):
    """Copy ``post`` as a new post; schedule it into free slots if configured.

    Returns ``(new_post, scheduled_count)``.
    """
    from apps.calendar.services import _next_available_slot
    from apps.growth.models import RecycledPost

    new_post = clone_post(post, author=author)
    new_post.title = post.title  # not "Copy of …": this is the same post again
    note = f"Recycled from a post published {post.published_at:%Y-%m-%d}." if post.published_at else "Recycled post."
    new_post.internal_notes = f"{note}\n\n{post.internal_notes}".strip()
    if growth_settings.recycle_ai_rewrite and ai.is_enabled() and post.caption:
        try:
            new_post.caption = ai.rewrite(post.caption)
        except ai.AIUnavailableError as exc:
            logger.info("Recycle rewrite skipped for post %s: %s", post.id, exc)
    new_post.save(update_fields=["title", "internal_notes", "caption", "updated_at"])

    RecycledPost.objects.create(
        workspace=post.workspace, source_post=post, new_post=new_post, source_score=score, automatic=automatic
    )

    scheduled = 0
    if growth_settings.recycle_auto_schedule:
        for pp in new_post.platform_posts.select_related("social_account"):
            if pp.social_account.needs_reconnect:
                continue
            slot = _next_available_slot(pp.social_account)
            if slot is None:
                continue
            try:
                transition_platform_post(pp, "scheduled", scheduled_at=slot)
                scheduled += 1
            except Exception as exc:  # approval gate, media no longer publishable, …
                logger.info("Recycled post %s left as draft on %s: %s", new_post.id, pp.social_account_id, exc)
    return new_post, scheduled


def run_recycling(workspace, growth_settings, *, force: bool = False) -> int:
    """The automatic weekly-quota run. Returns the number of posts recycled."""
    from apps.growth.models import RecycledPost

    now = timezone.now()
    if not growth_settings.recycle_enabled:
        return 0
    if not force and growth_settings.recycle_last_run_at and now - growth_settings.recycle_last_run_at < RUN_EVERY:
        return 0
    growth_settings.recycle_last_run_at = now
    growth_settings.save(update_fields=["recycle_last_run_at", "updated_at"])

    done_this_week = RecycledPost.objects.filter(
        workspace=workspace, automatic=True, created_at__gte=now - timedelta(days=7)
    ).count()
    # Spread the weekly quota: at most one a day, so posts don't bunch up.
    if done_this_week >= growth_settings.recycle_per_week:
        return 0
    candidates = recycle_candidates(workspace, growth_settings)
    if not candidates:
        return 0
    best = candidates[0]
    recycle_post(best.platform_post.post, growth_settings, score=best.lift, automatic=True)
    return 1
