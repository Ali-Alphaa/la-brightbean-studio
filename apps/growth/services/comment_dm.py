"""Instagram comment → DM automation.

When a comment on one of the workspace's Instagram posts contains a rule's
keyword, the commenter gets a private reply (a DM addressed by comment id)
with the rule's link, optionally a public reply on the comment, and a tag on
their audience contact.

Guarantees:

* a comment is answered at most once — ``CommentDMLog.comment_id`` is unique,
  and the log row is written before anything is sent;
* a rule sends at most ``daily_cap`` DMs per rolling 24 hours, and with
  ``once_per_user`` at most one per commenter, ever;
* comments older than Meta's 7-day private-reply window are skipped;
* :func:`handle_instagram_comment` (called from webhook and poll handlers)
  never raises.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

INSTAGRAM_PLATFORMS = ("instagram", "instagram_login")
PRIVATE_REPLY_WINDOW = timedelta(days=7)
MAX_KEYWORDS = 20


def parse_keywords(raw: str) -> list[str]:
    """Comma- or newline-separated keywords, trimmed, de-duplicated (case-insensitive)."""
    seen: dict[str, str] = {}
    for part in re.split(r"[,\n]", raw or ""):
        word = part.strip()[:100]
        if word and word.lower() not in seen:
            seen[word.lower()] = word
    return list(seen.values())[:MAX_KEYWORDS]


def match_keyword(text: str, keywords: list[str]) -> str | None:
    """The first keyword found in ``text`` as a whole word/phrase, case-insensitively."""
    for keyword in keywords:
        word = keyword.strip()
        if not word:
            continue
        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text or "", flags=re.IGNORECASE):
            return word
    return None


def _rules_for(account, media_id: str):
    from django.db.models import Q

    from apps.growth.models import CommentDMRule

    return CommentDMRule.objects.filter(social_account=account, enabled=True).filter(
        Q(media_id="") | Q(media_id=media_id)
    )


def _skip_reason(rule, *, commenter_id: str, commented_at: datetime | None, now: datetime) -> str:
    from apps.growth.models import CommentDMLog

    if commented_at and now - commented_at > PRIVATE_REPLY_WINDOW:
        return "Comment is older than Meta's 7-day private-reply window."
    counted = [CommentDMLog.Status.SENT, CommentDMLog.Status.PENDING]
    if (
        rule.once_per_user
        and commenter_id
        and CommentDMLog.objects.filter(rule=rule, commenter_id=commenter_id, status__in=counted).exists()
    ):
        return "Already sent to this person for this rule."
    sent_today = CommentDMLog.objects.filter(rule=rule, status__in=counted, created_at__gte=now - timedelta(days=1))
    if sent_today.count() >= rule.daily_cap:
        return f"Daily cap of {rule.daily_cap} reached."
    return ""


def record_match(
    account,
    *,
    comment_id: str,
    text: str,
    commenter_id: str = "",
    commenter_handle: str = "",
    media_id: str = "",
    commented_at: datetime | None = None,
    simulate: bool = False,
):
    """Match a comment against the account's rules and write the log row.

    Returns the new ``CommentDMLog`` (status pending, skipped or simulated),
    or None when no rule matched or the comment was already handled.
    """
    from apps.growth.models import CommentDMLog

    if account.platform not in INSTAGRAM_PLATFORMS or not comment_id:
        return None
    now = timezone.now()
    for rule in _rules_for(account, media_id):
        # Rules only answer comments written after they were created; the inbox
        # backfill of a newly connected account must not DM old commenters.
        if commented_at and commented_at < rule.created_at:
            continue
        keyword = match_keyword(text, rule.keywords)
        if keyword is None:
            continue
        reason = _skip_reason(rule, commenter_id=commenter_id, commented_at=commented_at, now=now)
        if simulate:
            status = CommentDMLog.Status.SIMULATED
        else:
            status = CommentDMLog.Status.SKIPPED if reason else CommentDMLog.Status.PENDING
        try:
            with transaction.atomic():
                return CommentDMLog.objects.create(
                    rule=rule,
                    comment_id=str(comment_id)[:255],
                    commenter_id=str(commenter_id)[:255],
                    commenter_handle=str(commenter_handle)[:255],
                    comment_text=(text or "")[:5000],
                    media_id=str(media_id)[:255],
                    keyword=keyword[:100],
                    status=status,
                    error=reason,
                    commented_at=commented_at,
                )
        except IntegrityError:
            return None  # this comment was already handled (webhook + poll, or a retry)
    return None


def handle_instagram_comment(account, **comment) -> None:
    """Webhook/poll entry point. Never raises; queues the send when a rule matched."""
    try:
        log = record_match(account, **comment)
        if log is not None and log.status == log.Status.PENDING:
            from apps.growth.tasks import send_comment_dm

            send_comment_dm(str(log.id))
    except Exception:
        logger.exception("Comment → DM handling failed for comment %s", comment.get("comment_id"))


def tag_contact(workspace, platform: str, handle: str, tag: str) -> None:
    from apps.growth.models import AudienceContact

    if not handle:
        return
    contact, _ = AudienceContact.objects.get_or_create(workspace=workspace, platform=platform, handle=handle[:255])
    if tag and tag not in contact.tags:
        contact.tags = [*contact.tags, tag][:20]
    contact.last_interaction_at = timezone.now()
    contact.save(update_fields=["tags", "last_interaction_at", "updated_at"])


def send(log) -> str:
    """Send the private reply (and public reply) for one pending log row. Never raises."""
    from apps.growth.models import CommentDMLog
    from apps.publisher.engine import _provider_and_access_token

    # Claim the row so a duplicate task can't send twice.
    claimed = CommentDMLog.objects.filter(id=log.id, status=CommentDMLog.Status.PENDING).update(
        status=CommentDMLog.Status.SENT
    )
    if not claimed:
        return CommentDMLog.objects.get(id=log.id).status
    rule = log.rule
    account = rule.social_account
    try:
        provider, token = _provider_and_access_token(account)
        result = provider.send_private_reply(token, log.comment_id, rule.dm_text)
        log.dm_message_id = str(result.platform_message_id or "")[:255]
        log.status = CommentDMLog.Status.SENT
    except Exception as exc:
        logger.warning("Comment → DM private reply for comment %s failed: %s", log.comment_id, exc)
        log.status = CommentDMLog.Status.FAILED
        log.error = str(exc)[:1000]
        log.save(update_fields=["status", "error"])
        return log.status
    if rule.public_reply_text.strip():
        try:
            reply = provider.reply_to_comment(token, log.comment_id, rule.public_reply_text.strip())
            log.public_reply_id = str(reply.platform_message_id or "")[:255]
        except Exception as exc:
            logger.info("Comment → DM public reply for comment %s failed: %s", log.comment_id, exc)
            log.error = f"DM sent; public reply failed: {exc}"[:1000]
    log.save(update_fields=["status", "dm_message_id", "public_reply_id", "error"])
    try:
        tag_contact(rule.workspace, account.platform, log.commenter_id or log.commenter_handle, rule.tag)
    except Exception:
        logger.exception("Tagging commenter for comment %s failed", log.comment_id)
    return log.status
