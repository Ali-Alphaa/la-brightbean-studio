"""Repurpose inspiration posts into exactly 5 new Instagram/TikTok posts, then hand one to the composer.

Publishing itself is never reimplemented: the chosen variation becomes a draft
through :func:`apps.composer.services.create_post` and the user attaches media
and schedules it in the existing composer (both networks require media).
"""

from __future__ import annotations

import logging

from django.db import transaction

from . import ai
from .voice import best_samples

logger = logging.getLogger(__name__)

MAX_SOURCES = 10
ACCOUNT_PLATFORMS = {"instagram": ("instagram", "instagram_login"), "tiktok": ("tiktok",)}


class RepurposeError(Exception):
    pass


def generate(workspace, posts, *, platform: str, voice=None, user=None):
    """Ask the AI for 5 variations of ``posts``; record and return the ``RepurposeRun``."""
    from apps.growth.models import RepurposeRun

    posts = list(posts)
    if not 1 <= len(posts) <= MAX_SOURCES:
        raise RepurposeError(f"Select between 1 and {MAX_SOURCES} posts.")
    if platform not in ACCOUNT_PLATFORMS:
        raise RepurposeError("Pick Instagram or TikTok.")
    sources = [
        {"author": p.author_handle, "caption": p.caption[:3000], "media_type": p.media_type, "platform": p.platform}
        for p in posts
    ]
    variations = ai.repurpose(
        sources,
        platform=platform,
        style_guide=voice.style_guide if voice else "",
        voice_samples=[s["text"] for s in best_samples(voice, 10)] if voice else None,
    )
    return RepurposeRun.objects.create(
        workspace=workspace,
        target_platform=platform,
        source_ids=[p.id for p in posts],
        voice=voice,
        variations=variations,
        created_by=user,
    )


def caption_for(variation: dict, platform: str) -> str:
    caption = variation.get("caption", "").strip()
    hook = variation.get("hook", "").strip()
    if platform == "instagram" and hook and not caption.startswith(hook):
        caption = f"{hook}\n\n{caption}"
    return caption


def notes_for(run, variation: dict) -> str:
    lines = [f"Repurposed from {len(run.source_ids)} inspiration post(s) in Growth → Inspiration."]
    if variation.get("hook"):
        lines.append(f"Hook: {variation['hook']}")
    if variation.get("script"):
        lines.append("Video script:")
        lines.extend(f"- {beat}" for beat in variation["script"])
    return "\n".join(lines)


def choose(run, index: int, *, caption: str, social_account, user=None, propose_next_slot: bool = False):
    """Turn variation ``index`` (possibly edited) into a draft for ``social_account``. Returns the Post."""
    from apps.composer.services import create_post

    if not 0 <= index < len(run.variations):
        raise RepurposeError("Pick one of the variations.")
    if social_account.workspace_id != run.workspace_id:
        raise RepurposeError("That account isn't in this workspace.")
    if social_account.platform not in ACCOUNT_PLATFORMS[run.target_platform]:
        raise RepurposeError(f"Pick a connected {run.get_target_platform_display()} account.")
    caption = (caption or "").strip() or caption_for(run.variations[index], run.target_platform)
    proposed = None
    if propose_next_slot:
        from apps.calendar.services import _next_available_slot

        proposed = _next_available_slot(social_account)
    with transaction.atomic():
        post = create_post(
            workspace=run.workspace,
            social_account=social_account,
            caption=caption,
            title=caption.splitlines()[0][:255] if caption else "Repurposed post",
            internal_notes=notes_for(run, run.variations[index]),
            proposed_publish_at=proposed,
            status="draft",
            author=user,
        )
        run.chosen_index = index
        run.post = post
        variations = list(run.variations)
        variations[index] = {**variations[index], "caption": caption}
        run.variations = variations
        run.save(update_fields=["chosen_index", "post", "variations"])
    return post


def tiktok_privacy(account) -> dict:
    """What TikTok currently allows this account to post as.

    ``self_only`` is True when ``creator_info`` offers only SELF_ONLY — the app
    hasn't passed TikTok's content-posting audit, so the provider will publish
    the post privately. ``options`` is None when TikTok couldn't be asked.
    """
    from apps.publisher.engine import _provider_and_access_token

    try:
        provider, token = _provider_and_access_token(account)
        info = provider.query_creator_info(token)
    except Exception as exc:
        logger.info("TikTok creator_info for %s failed: %s", account.id, exc)
        return {"options": None, "self_only": None}
    options = list(info.get("privacy_level_options") or [])
    return {"options": options, "self_only": options == ["SELF_ONLY"]}
