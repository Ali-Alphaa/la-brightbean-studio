"""Auto-plug: comment on your own post once it takes off.

The plug (a link to a newsletter, product, …) is held back until the post
crosses an engagement threshold, so it rides on a post that's already getting
attention instead of weighing down one that isn't.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.analytics.metrics import post_metrics_for
from apps.analytics.services import _latest_post_stats
from apps.composer.models import PlatformPost

from .analysis import engagement_total
from .utm import tag_for_platform_post

logger = logging.getLogger(__name__)

#: Posts rarely gain much after this; stop watching.
PLUG_WINDOW = timedelta(days=7)


def attach_default_plugs(growth_settings) -> int:
    """Give each recent published post in the workspace the default plug."""
    from apps.growth.models import AutoPlug

    if not (growth_settings.autoplug_enabled and growth_settings.autoplug_text.strip()):
        return 0
    since = timezone.now() - PLUG_WINDOW
    if growth_settings.autoplug_enabled_at:
        since = max(since, growth_settings.autoplug_enabled_at)
    candidates = PlatformPost.objects.filter(
        post__workspace_id=growth_settings.workspace_id,
        status=PlatformPost.Status.PUBLISHED,
        published_at__gte=since,
        auto_plug__isnull=True,
    )
    plugs = [
        AutoPlug(platform_post=pp, text=growth_settings.autoplug_text, threshold=growth_settings.autoplug_threshold)
        for pp in candidates
    ]
    AutoPlug.objects.bulk_create(plugs, ignore_conflicts=True)
    return len(plugs)


def process_plug(plug) -> str:
    """Check one pending plug and post it if its post has crossed the threshold."""
    from apps.growth.models import AutoPlug
    from apps.publisher.engine import _provider_and_access_token

    pp = plug.platform_post
    if pp.status != PlatformPost.Status.PUBLISHED or not pp.platform_post_id:
        return plug.status
    if pp.published_at and timezone.now() - pp.published_at > PLUG_WINDOW:
        plug.status = AutoPlug.Status.EXPIRED
        plug.save(update_fields=["status"])
        return plug.status

    stats = _latest_post_stats([pp.id], post_metrics_for(pp.social_account.platform)).get(pp.id, {})
    plug.last_engagements = engagement_total(stats)
    if plug.last_engagements < plug.threshold:
        plug.save(update_fields=["last_engagements"])
        return plug.status

    text = tag_for_platform_post(plug.text, pp)
    try:
        provider, access_token = _provider_and_access_token(pp.social_account)
        result = provider.publish_comment(access_token=access_token, post_id=pp.platform_post_id, text=text)
    except NotImplementedError:
        plug.status = AutoPlug.Status.FAILED
        plug.error = f"{pp.social_account.get_platform_display()} doesn't support comments through its API."
    except Exception as exc:
        # Not retried: if the comment landed despite the error, a retry would
        # post the plug twice on a live post.
        logger.warning("Auto-plug %s failed: %s", plug.id, exc)
        plug.status = AutoPlug.Status.FAILED
        plug.error = str(exc)[:1000]
    else:
        plug.status = AutoPlug.Status.POSTED
        plug.comment_id = str(getattr(result, "comment_id", "") or "")[:255]
        plug.posted_at = timezone.now()
    plug.save(update_fields=["status", "error", "comment_id", "posted_at", "last_engagements"])
    return plug.status


def process_pending_plugs() -> int:
    from apps.growth.models import AutoPlug, GrowthSettings

    for growth_settings in GrowthSettings.objects.filter(autoplug_enabled=True):
        attach_default_plugs(growth_settings)

    posted = 0
    pending = AutoPlug.objects.filter(status=AutoPlug.Status.PENDING).select_related(
        "platform_post__social_account", "platform_post__post"
    )
    for plug in pending:
        try:
            posted += int(process_plug(plug) == AutoPlug.Status.POSTED)
        except Exception:
            logger.exception("Processing auto-plug %s failed", plug.id)
    return posted
