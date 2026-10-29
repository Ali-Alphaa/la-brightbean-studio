"""Growth pages for Instagram + TikTok: Inspiration library, repurpose, voices, comment → DM."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from .models import CommentDMLog, CommentDMRule, GrowthVoice, InspirationPost, RepurposeRun
from .services import ai, comment_dm, inspiration, repurpose, voice
from .views import _int, _render

logger = logging.getLogger(__name__)

IG_TIKTOK = ("instagram", "instagram_login", "tiktok")
DISCOVERY_CACHE_SECONDS = 30 * 60
SORTS = {"ratio": ("ratio", "likes"), "likes": ("likes",), "recent": ("captured_at",)}


def _can_edit(request) -> bool:
    return bool(request.workspace_membership.effective_permissions.get("create_posts"))


def _social_accounts(workspace, platforms=IG_TIKTOK):
    return list(
        SocialAccount.objects.filter(workspace=workspace, platform__in=platforms).order_by("platform", "account_name")
    )


def _go(name: str, workspace, query: str = "") -> HttpResponse:
    target = reverse(f"growth:{name}", kwargs={"workspace_id": workspace.id})
    return redirect(f"{target}?{query}" if query else target)


# --- inspiration library ------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def inspiration_library(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    f: dict[str, Any] = {
        "platform": request.GET.get("platform", ""),
        "q": request.GET.get("q", "").strip()[:200],
        "topic": request.GET.get("topic", "").strip()[:40],
        "hook": request.GET.get("hook", ""),
        "format": request.GET.get("format", ""),
        "niche": request.GET.get("niche", "on"),
        "source": request.GET.get("source", ""),
        "sort": request.GET.get("sort", "ratio"),
        "hidden": request.GET.get("hidden") == "1",
    }
    qs = InspirationPost.objects.filter(workspace=workspace, hidden=f["hidden"])
    if f["platform"] in ("instagram", "tiktok"):
        qs = qs.filter(platform=f["platform"])
    if f["q"]:
        qs = qs.filter(Q(caption__icontains=f["q"]) | Q(author_handle__icontains=f["q"]))
    if f["topic"]:
        qs = qs.filter(topics__contains=[f["topic"].lower()])
    if f["hook"] in InspirationPost.HOOK_TYPES:
        qs = qs.filter(hook_type=f["hook"])
    if f["format"] in InspirationPost.FORMATS:
        qs = qs.filter(format=f["format"])
    if f["niche"] == "on":  # untagged posts stay visible until they're analyzed
        qs = qs.exclude(on_niche=False)
    elif f["niche"] == "off":
        qs = qs.filter(on_niche=False)
    if f["source"] in InspirationPost.Source.values:
        qs = qs.filter(source=f["source"])
    ordering = [F(field).desc(nulls_last=True) for field in SORTS.get(f["sort"], SORTS["ratio"])]
    posts = list(qs.order_by(*ordering, "-captured_at")[:200])
    all_topics = sorted(
        {
            t
            for topics in InspirationPost.objects.filter(workspace=workspace).values_list("topics", flat=True)
            for t in topics
        }
    )
    return _render(
        request,
        "growth/inspiration.html",
        "inspiration",
        {
            "posts": posts,
            "f": f,
            "all_topics": all_topics,
            "hook_types": InspirationPost.HOOK_TYPES,
            "formats": InspirationPost.FORMATS,
            "sources": InspirationPost.Source.choices,
            "pending_count": InspirationPost.objects.filter(
                workspace=workspace, analyzed_at__isnull=True, hidden=False
            ).count(),
            "total_count": InspirationPost.objects.filter(workspace=workspace).count(),
            "voices": GrowthVoice.objects.filter(workspace=workspace),
            "discovery_account": inspiration.discovery_account(workspace),
            "ai_backend": ai.backend(),
            "api_keys_url": reverse("api_keys:list"),
            "api_base": request.build_absolute_uri("/api/v1/"),
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_add_link(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    try:
        post, note = inspiration.save_by_link(
            workspace, request.POST.get("url", ""), pasted_caption=request.POST.get("caption", "")[:10000]
        )
    except inspiration.InspirationError as exc:
        messages.error(request, str(exc))
    else:
        if note:
            messages.warning(request, note)
        messages.success(request, f"Saved {post.get_platform_display()} post to the library.")
        from .tasks import queue_tagging

        queue_tagging(workspace)
    return _go("inspiration", workspace, "sort=recent")


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_hide(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    post = get_object_or_404(InspirationPost, id=post_id, workspace=request.workspace)
    post.hidden = not post.hidden
    post.save(update_fields=["hidden"])
    return redirect(request.POST.get("next") or reverse("growth:inspiration", kwargs={"workspace_id": workspace_id}))


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_analyze(request: HttpRequest, workspace_id) -> HttpResponse:
    if not ai.is_enabled():
        messages.error(request, "AI is not configured. Set ANTHROPIC_API_KEY (or GROWTH_AI_API_KEY) first.")
    else:
        try:
            tagged = inspiration.analyze_pending(request.workspace, max_batches=3)
        except ai.AIUnavailableError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Tagged {tagged} post(s).")
    return _go("inspiration", request.workspace)


@login_required
@require_permission("view_analytics")
@require_POST
def inspiration_discover(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    try:
        profile, items = inspiration.discover_instagram(workspace, request.POST.get("handle", ""))
    except inspiration.InspirationError as exc:
        messages.error(request, str(exc))
        return _go("inspiration", workspace)
    token = uuid.uuid4().hex
    cache.set(f"growth:discovery:{workspace.id}:{token}", items, DISCOVERY_CACHE_SECONDS)
    known = set(
        InspirationPost.objects.filter(
            workspace=workspace, platform="instagram", external_id__in=[i["external_id"] for i in items]
        ).values_list("external_id", flat=True)
    )
    return _render(
        request,
        "growth/inspiration_discover.html",
        "inspiration",
        {"profile": profile, "items": items, "token": token, "known": known},
    )


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_discover_save(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    token = request.POST.get("token", "")[:64]
    items = cache.get(f"growth:discovery:{workspace.id}:{token}") if token.isalnum() else None
    if items is None:
        messages.error(request, "Those results expired. Collect the account again.")
        return _go("inspiration", workspace)
    wanted = set(request.POST.getlist("selected"))
    saved = inspiration.save_discovered(workspace, [i for i in items if i["external_id"] in wanted])
    messages.success(request, f"Saved {saved} post(s) to the library.")
    if saved:
        from .tasks import queue_tagging

        queue_tagging(workspace)
    return _go("inspiration", workspace, "source=discovery")


def _selected_voice(request, workspace):
    if request.POST.get("use_voice") != "on":
        return None
    voice_id = request.POST.get("voice", "")
    if voice_id.isdigit():
        return GrowthVoice.objects.filter(workspace=workspace, id=int(voice_id)).first()
    return None


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_repurpose(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    ids = [int(i) for i in request.POST.getlist("selected") if i.isdigit()][: repurpose.MAX_SOURCES + 1]
    posts = list(InspirationPost.objects.filter(workspace=workspace, id__in=ids))
    platform = request.POST.get("platform", "instagram")
    if not ai.is_enabled():
        messages.error(request, "AI is not configured. Set ANTHROPIC_API_KEY (or GROWTH_AI_API_KEY) first.")
        return _go("inspiration", workspace)
    try:
        run = repurpose.generate(
            workspace, posts, platform=platform, voice=_selected_voice(request, workspace), user=request.user
        )
    except (repurpose.RepurposeError, ai.AIUnavailableError) as exc:
        messages.error(request, str(exc))
        return _go("inspiration", workspace)
    return redirect("growth:repurpose_detail", workspace_id=workspace.id, run_id=run.id)


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_rewrite(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    """ "Rewrite in my voice": one rewrite of one post, shown like a repurpose run."""
    workspace = request.workspace
    post = get_object_or_404(InspirationPost, id=post_id, workspace=workspace)
    voice_obj = _selected_voice(request, workspace)
    platform = request.POST.get("platform") or post.platform
    platform = platform if platform in ("instagram", "tiktok") else "instagram"
    try:
        text = ai.rewrite(
            post.caption,
            platform=platform,
            style_guide=voice_obj.style_guide if voice_obj else "",
            voice_samples=[s["text"] for s in voice.best_samples(voice_obj, 10)] if voice_obj else None,
        )
    except ai.AIUnavailableError as exc:
        messages.error(request, str(exc))
        return _go("inspiration", workspace)
    run = RepurposeRun.objects.create(
        workspace=workspace,
        target_platform=platform,
        source_ids=[post.id],
        voice=voice_obj,
        variations=[{"hook": "", "caption": text, "script": []}],
        created_by=request.user,
    )
    return redirect("growth:repurpose_detail", workspace_id=workspace.id, run_id=run.id)


@login_required
@require_permission("create_posts")
def repurpose_detail(request: HttpRequest, workspace_id, run_id) -> HttpResponse:
    workspace = request.workspace
    run = get_object_or_404(RepurposeRun.objects.select_related("voice", "post"), id=run_id, workspace=workspace)
    accounts = _social_accounts(workspace, repurpose.ACCOUNT_PLATFORMS[run.target_platform])
    default_account = None
    if run.voice and run.voice.social_account_id:
        default_account = next((a for a in accounts if a.id == run.voice.social_account_id), None)
    default_account = default_account or (accounts[0] if accounts else None)
    privacy = None
    if run.target_platform == "tiktok" and default_account is not None:
        privacy = repurpose.tiktok_privacy(default_account)
    return _render(
        request,
        "growth/repurpose.html",
        "inspiration",
        {
            "run": run,
            "cards": [
                {"index": i, "variation": v, "caption": repurpose.caption_for(v, run.target_platform)}
                for i, v in enumerate(run.variations)
            ],
            "sources": InspirationPost.objects.filter(workspace=workspace, id__in=run.source_ids),
            "accounts": accounts,
            "default_account": default_account,
            "limit": ai.CAPTION_LIMITS[run.target_platform],
            "privacy": privacy,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def repurpose_choose(request: HttpRequest, workspace_id, run_id) -> HttpResponse:
    workspace = request.workspace
    run = get_object_or_404(RepurposeRun, id=run_id, workspace=workspace)
    account = get_object_or_404(SocialAccount, id=request.POST.get("account"), workspace=workspace)
    index = _int(request.POST.get("index"), -1, low=-1, high=100)
    try:
        post = repurpose.choose(
            run,
            index,
            caption=request.POST.get("caption", "")[:5000],
            social_account=account,
            user=request.user,
            propose_next_slot=request.POST.get("action") == "schedule",
        )
    except (repurpose.RepurposeError, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("growth:repurpose_detail", workspace_id=workspace.id, run_id=run.id)
    messages.success(request, "Draft created. Attach your image or video, then schedule it.")
    return redirect("composer:compose_edit", workspace_id=workspace.id, post_id=post.id)


# --- voices -------------------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def voices(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    accounts = _social_accounts(workspace)
    # Every connected IG/TikTok account gets a chip, with or without a voice yet.
    for account in accounts:
        handle = voice.voice_handle(account)
        if handle:
            GrowthVoice.objects.get_or_create(
                workspace=workspace,
                platform=voice.voice_platform(account),
                handle=handle,
                defaults={"social_account": account},
            )
    all_voices = list(GrowthVoice.objects.filter(workspace=workspace).select_related("social_account"))
    wanted = request.GET.get("voice", "")
    selected = next((v for v in all_voices if str(v.id) == wanted), all_voices[0] if all_voices else None)
    return _render(
        request,
        "growth/voices.html",
        "voices",
        {
            "voices": all_voices,
            "selected": selected,
            "best": voice.best_samples(selected, 10) if selected else [],
            "ai_backend": ai.backend(),
        },
    )


def _voice_or_404(request, voice_id):
    return get_object_or_404(GrowthVoice, id=voice_id, workspace=request.workspace)


@login_required
@require_permission("create_posts")
@require_POST
def voice_save(request: HttpRequest, workspace_id, voice_id) -> HttpResponse:
    v = _voice_or_404(request, voice_id)
    v.style_guide = request.POST.get("style_guide", "")[:10000]
    v.save(update_fields=["style_guide", "updated_at"])
    messages.success(request, "Style guide saved.")
    return _go("voices", request.workspace, f"voice={v.id}")


@login_required
@require_permission("create_posts")
@require_POST
def voice_regenerate(request: HttpRequest, workspace_id, voice_id) -> HttpResponse:
    v = _voice_or_404(request, voice_id)
    try:
        voice.regenerate_style_guide(v)
    except ai.AIUnavailableError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Style guide regenerated from your best posts. Edit it as you like.")
    return _go("voices", request.workspace, f"voice={v.id}")


@login_required
@require_permission("create_posts")
@require_POST
def voice_pull(request: HttpRequest, workspace_id, voice_id) -> HttpResponse:
    v = _voice_or_404(request, voice_id)
    if v.social_account is None:
        messages.error(request, "This voice isn't linked to a connected account; add posts with the browser extension.")
    else:
        try:
            v, count = voice.pull_recent_posts(v.social_account)
        except inspiration.InspirationError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Pulled {count} recent post(s).")
    return _go("voices", request.workspace, f"voice={v.id}")


# --- comment → DM -------------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
def comment_dm_rules(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    accounts = _social_accounts(workspace, comment_dm.INSTAGRAM_PLATFORMS)
    if request.method == "POST":
        if not _can_edit(request):
            messages.error(request, "You don't have permission to add rules.")
            return _go("comment_dm", workspace)
        account = next((a for a in accounts if str(a.id) == request.POST.get("account")), None)
        keywords = comment_dm.parse_keywords(request.POST.get("keywords", ""))
        dm_text = request.POST.get("dm_text", "").strip()[:1000]
        if account is None:
            messages.error(request, "Pick a connected Instagram account.")
        elif not keywords:
            messages.error(request, "Add at least one keyword.")
        elif not dm_text or len(dm_text.encode("utf-8")) > 1000:
            messages.error(request, "Write the DM (up to 1,000 bytes).")
        else:
            CommentDMRule.objects.create(
                workspace=workspace,
                social_account=account,
                media_id=request.POST.get("media_id", "").strip()[:255],
                name=request.POST.get("name", "").strip()[:200],
                keywords=keywords,
                dm_text=dm_text,
                public_reply_text=request.POST.get("public_reply_text", "").strip()[:2000],
                tag=request.POST.get("tag", "").strip()[:50],
                daily_cap=_int(request.POST.get("daily_cap"), 200, low=1, high=1000),
                once_per_user=request.POST.get("once_per_user") == "on",
            )
            messages.success(request, "Rule saved. It runs on new comments from now on.")
        return _go("comment_dm", workspace)

    rules = CommentDMRule.objects.filter(workspace=workspace).select_related("social_account")
    return _render(
        request,
        "growth/comment_dm.html",
        "comment_dm",
        {
            "accounts": accounts,
            "rules": rules,
            "logs": CommentDMLog.objects.filter(rule__workspace=workspace).select_related("rule")[:50],
            "simulation": request.session.pop("growth_cdm_simulation", None),
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def comment_dm_toggle(request: HttpRequest, workspace_id, rule_id) -> HttpResponse:
    rule = get_object_or_404(CommentDMRule, id=rule_id, workspace=request.workspace)
    if request.POST.get("action") == "delete":
        rule.delete()
    else:
        rule.enabled = not rule.enabled
        rule.save(update_fields=["enabled"])
    return _go("comment_dm", request.workspace)


@login_required
@require_permission("use_inbox")
@require_POST
def comment_dm_simulate(request: HttpRequest, workspace_id, rule_id) -> HttpResponse:
    """Dry run: show what a comment would trigger. Writes a "simulated" log row, sends nothing."""
    rule = get_object_or_404(
        CommentDMRule.objects.select_related("social_account"), id=rule_id, workspace=request.workspace
    )
    text = request.POST.get("text", "")[:2000]
    log = comment_dm.record_match(
        rule.social_account,
        comment_id=f"sim-{uuid.uuid4().hex}",
        text=text,
        commenter_id=f"sim-{request.POST.get('handle', 'tester')[:50]}",
        commenter_handle=request.POST.get("handle", "tester")[:50],
        media_id=rule.media_id,
        simulate=True,
    )
    if log is None:
        result = {"matched": False, "text": text}
    else:
        result = {
            "matched": True,
            "text": text,
            "rule": str(log.rule),
            "keyword": log.keyword,
            "dm": log.rule.dm_text,
            "public_reply": log.rule.public_reply_text,
            "tag": log.rule.tag,
            "note": log.error,
        }
    request.session["growth_cdm_simulation"] = result
    return _go("comment_dm", request.workspace)
