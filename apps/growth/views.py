"""Growth pages: best times, hashtags, top posts & recycling, competitors, A/B tests, audience, settings."""

from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.common.validators import parse_and_truncate_tag_string
from apps.composer.models import Idea, PlatformPost, Post
from apps.members.decorators import require_permission
from apps.social_accounts.models import SocialAccount

from .models import (
    AudienceContact,
    AutoPlug,
    CaptionExperiment,
    Competitor,
    CompetitorPost,
    GrowthSettings,
    RecycledPost,
)
from .services import ai, analysis, audience, autoplug, competitors, experiments, recycle

logger = logging.getLogger(__name__)

MAX_COMPETITORS = 25


# --- helpers -------------------------------------------------------------------


def _accounts(workspace):
    return list(SocialAccount.objects.filter(workspace=workspace).order_by("platform", "account_name"))


def _selected_account(request, accounts):
    wanted = request.GET.get("account") or request.POST.get("account") or ""
    for account in accounts:
        if str(account.id) == wanted:
            return account
    return accounts[0] if accounts else None


def _render(request, template, tab, context):
    workspace = request.workspace
    base = {"workspace": workspace, "tab": tab, "ai_enabled": ai.is_enabled()}
    return render(request, template, {**base, **context})


def _int(value, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _workspace_tz(workspace) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(workspace.effective_timezone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo("UTC")


def _scored_for(accounts, days=analysis.DEFAULT_LOOKBACK_DAYS):
    if not accounts:
        return []
    return analysis.score_posts(analysis.published_platform_posts(accounts, days=days))


# --- index & best times ------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def index(request: HttpRequest, workspace_id) -> HttpResponse:
    return redirect("growth:best_times", workspace_id=workspace_id)


@login_required
@require_permission("view_analytics")
@require_GET
def best_times(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    accounts = _accounts(workspace)
    account = _selected_account(request, accounts)
    result = None
    if account:
        result = analysis.best_times(_scored_for([account]), workspace.effective_timezone or "UTC")
    heat_rows = []
    if result:
        max_lift = max([cell[1] for row in result.grid for cell in row if cell] + [1.0])
        for day, row in zip(analysis.DAY_NAMES, result.grid, strict=True):
            cells = []
            for cell in row:
                if cell is None:
                    cells.append(None)
                    continue
                posts, lift = cell
                # Above-typical hours shade by how far above; below-typical are flat grey.
                alpha = round(0.15 + 0.85 * (lift - 1) / (max_lift - 1), 2) if lift > 1 and max_lift > 1 else 0
                cells.append({"posts": posts, "lift": lift, "alpha": alpha})
            heat_rows.append((day, cells))
    return _render(
        request,
        "growth/best_times.html",
        "best_times",
        {
            "accounts": accounts,
            "account": account,
            "result": result,
            "heat_rows": heat_rows,
            "hours": range(24),
            "min_posts": analysis.MIN_POSTS_FOR_BEST_TIMES,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def apply_best_times(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    account = get_object_or_404(SocialAccount, id=request.POST.get("account"), workspace=workspace)
    result = analysis.best_times(_scored_for([account]), workspace.effective_timezone or "UTC")
    count = _int(request.POST.get("count"), 5, low=1, high=len(result.top))
    added = analysis.apply_best_times(account, result.top[:count], replace=request.POST.get("replace") == "on")
    messages.success(request, f"Posting slots updated for {account.account_name} ({added} added).")
    return redirect(f"{reverse('growth:best_times', kwargs={'workspace_id': workspace.id})}?account={account.id}")


# --- hashtags --------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def hashtags(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    accounts = _accounts(workspace)
    account = _selected_account(request, accounts)
    scored = _scored_for([account] if account else [])
    stats = analysis.hashtag_stats(scored)
    context = {
        "accounts": accounts,
        "account": account,
        "best": [h for h in stats if h.uses >= 2][:20],
        "worst": sorted([h for h in stats if h.uses >= 2 and h.lift < 1.0], key=lambda h: h.lift)[:10],
        "scored_count": len(scored),
        "caption": "",
        "history_suggestions": None,
    }
    if request.method == "POST":
        caption = request.POST.get("caption", "")[:5000]
        context["caption"] = caption
        context["history_suggestions"] = analysis.suggest_hashtags_from_history(caption, scored)
        if request.POST.get("use_ai") and ai.is_enabled():
            try:
                context["ai_suggestions"] = ai.suggest_hashtags(
                    caption,
                    platform=account.platform if account else "",
                    known_good=[h.tag for h in context["best"][:10]],
                )
            except ai.AIUnavailableError as exc:
                context["ai_error"] = str(exc)
        if request.headers.get("HX-Request"):
            return _render(request, "growth/_hashtag_suggestions.html", "hashtags", context)
    return _render(request, "growth/hashtags.html", "hashtags", context)


# --- top posts & recycling -----------------------------------------------------------


@login_required
@require_permission("view_analytics")
@require_GET
def top_posts(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    growth_settings = GrowthSettings.for_workspace(workspace)
    accounts = _accounts(workspace)
    best = analysis.top_posts(
        _scored_for(accounts, days=365), top_percent=growth_settings.recycle_top_percent, limit=30
    )
    candidate_ids = {s.platform_post.post_id for s in recycle.recycle_candidates(workspace, growth_settings)}
    return _render(
        request,
        "growth/top_posts.html",
        "top_posts",
        {
            "settings": growth_settings,
            "best": best,
            "candidate_ids": candidate_ids,
            "history": RecycledPost.objects.filter(workspace=workspace).select_related("source_post", "new_post")[:20],
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def recycle_now(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    workspace = request.workspace
    post = get_object_or_404(Post, id=post_id, workspace=workspace)
    growth_settings = GrowthSettings.for_workspace(workspace)
    new_post, scheduled = recycle.recycle_post(post, growth_settings, author=request.user, automatic=False)
    if scheduled:
        messages.success(request, f"Recycled and scheduled on {scheduled} channel(s).")
    else:
        messages.success(request, "Recycled as a draft. Open it from the calendar to schedule.")
    return redirect("growth:top_posts", workspace_id=workspace.id)


# --- competitors & inspiration ------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def competitor_list(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    if request.method == "POST":
        if not request.workspace_membership.effective_permissions.get("create_posts"):
            messages.error(request, "You don't have permission to add competitors.")
            return redirect("growth:competitors", workspace_id=workspace.id)
        platform = request.POST.get("platform", "")
        try:
            handle = competitors.normalize_handle(platform, request.POST.get("handle", ""))
            if Competitor.objects.filter(workspace=workspace).count() >= MAX_COMPETITORS:
                raise competitors.CompetitorError(f"You can track up to {MAX_COMPETITORS} accounts.")
            with transaction.atomic():  # savepoint: a duplicate must not poison the request's transaction
                competitor = Competitor.objects.create(workspace=workspace, platform=platform, handle=handle)
        except competitors.CompetitorError as exc:
            messages.error(request, str(exc))
        except IntegrityError:
            messages.info(request, "You're already tracking that account.")
        else:
            competitors.sync_competitor(competitor)
            if competitor.last_error:
                messages.warning(request, f"Added, but the first sync failed: {competitor.last_error}")
            else:
                messages.success(request, f"Now tracking {competitor.display_name or competitor.handle}.")
        return redirect("growth:competitors", workspace_id=workspace.id)

    rows = []
    for competitor in Competitor.objects.filter(workspace=workspace):
        rows.append(
            {
                "competitor": competitor,
                "delta_7": competitors.follower_delta(competitor, 7),
                "delta_30": competitors.follower_delta(competitor, 30),
            }
        )
    days = _int(request.GET.get("days"), 30, low=1, high=365)
    inspiration = (
        CompetitorPost.objects.filter(
            competitor__workspace=workspace, published_at__gte=timezone.now() - timedelta(days=days)
        )
        .select_related("competitor")
        .order_by("-engagements")[:30]
    )
    return _render(
        request,
        "growth/competitors.html",
        "competitors",
        {
            "rows": rows,
            "inspiration": inspiration,
            "days": days,
            "platforms": Competitor.Platform.choices,
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def competitor_refresh(request: HttpRequest, workspace_id, competitor_id) -> HttpResponse:
    competitor = get_object_or_404(Competitor, id=competitor_id, workspace=request.workspace)
    competitors.sync_competitor(competitor)
    if competitor.last_error:
        messages.error(request, competitor.last_error)
    return redirect("growth:competitors", workspace_id=workspace_id)


@login_required
@require_permission("create_posts")
@require_POST
def competitor_delete(request: HttpRequest, workspace_id, competitor_id) -> HttpResponse:
    get_object_or_404(Competitor, id=competitor_id, workspace=request.workspace).delete()
    return redirect("growth:competitors", workspace_id=workspace_id)


@login_required
@require_permission("create_posts")
@require_POST
def inspiration_to_idea(request: HttpRequest, workspace_id, post_id) -> HttpResponse:
    source = get_object_or_404(CompetitorPost, id=post_id, competitor__workspace=request.workspace)
    description = f"{source.text}\n\nInspired by {source.competitor.handle}: {source.url}"
    if request.POST.get("rewrite") and ai.is_enabled():
        try:
            description = f"{ai.rewrite(source.text)}\n\nInspired by {source.competitor.handle}: {source.url}"
        except ai.AIUnavailableError as exc:
            messages.warning(request, f"Saved without a rewrite: {exc}")
    first_line = (source.text.strip().splitlines() or ["Inspiration"])[0]
    Idea.objects.create(
        workspace=request.workspace,
        author=request.user,
        title=first_line[:255],
        description=description,
    )
    messages.success(request, "Saved to your idea board.")
    return redirect("growth:competitors", workspace_id=workspace_id)


# --- A/B experiments --------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def experiment_list(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    accounts = [a for a in _accounts(workspace) if not a.needs_reconnect]
    tz = _workspace_tz(workspace)
    form = {
        "name": "",
        "captions": ["", ""],
        "start_at": (timezone.now() + timedelta(days=1)).astimezone(tz).strftime("%Y-%m-%dT09:00"),
        "spacing_hours": 24,
        "evaluate_after_hours": 48,
        "account": str(accounts[0].id) if accounts else "",
    }

    if request.method == "POST":
        if not request.workspace_membership.effective_permissions.get("create_posts"):
            messages.error(request, "You don't have permission to create tests.")
            return redirect("growth:experiments", workspace_id=workspace.id)
        form.update(
            {
                "name": request.POST.get("name", "")[:200],
                "captions": [c[:5000] for c in request.POST.getlist("caption")][: experiments.MAX_VARIANTS],
                "start_at": request.POST.get("start_at", ""),
                "spacing_hours": _int(request.POST.get("spacing_hours"), 24, low=1, high=24 * 14),
                "evaluate_after_hours": _int(request.POST.get("evaluate_after_hours"), 48, low=12, high=24 * 14),
                "account": request.POST.get("account", ""),
            }
        )
        account = next((a for a in accounts if str(a.id) == form["account"]), None)
        if request.POST.get("action") == "generate":
            base = next((c for c in form["captions"] if c.strip()), "")
            if not base:
                messages.error(request, "Write caption A first, then generate variants from it.")
            else:
                try:
                    extra = ai.caption_variants(base, count=2, platform=account.platform if account else "")
                    form["captions"] = [base, *extra][: experiments.MAX_VARIANTS]
                except ai.AIUnavailableError as exc:
                    messages.error(request, str(exc))
        elif account is None:
            messages.error(request, "Pick a connected account.")
        else:
            try:
                start_at = datetime.fromisoformat(form["start_at"]).replace(tzinfo=tz)
                experiment = experiments.create_experiment(
                    workspace=workspace,
                    social_account=account,
                    name=form["name"],
                    captions=form["captions"],
                    start_at=start_at,
                    spacing_hours=form["spacing_hours"],
                    evaluate_after_hours=form["evaluate_after_hours"],
                    schedule=request.POST.get("as_drafts") != "on",
                    author=request.user,
                )
            except ValueError as exc:  # bad datetime, approval gate, unpublishable account
                messages.error(request, str(exc) or "Check the start time.")
            except experiments.ExperimentError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Test created. Variants are on the calendar.")
                return redirect("growth:experiment_detail", workspace_id=workspace.id, experiment_id=experiment.id)
        while len(form["captions"]) < 2:
            form["captions"].append("")

    return _render(
        request,
        "growth/experiments.html",
        "experiments",
        {
            "accounts": accounts,
            "form": form,
            "experiment_rows": [
                {"experiment": e, "progress": experiments.experiment_progress(e)}
                for e in CaptionExperiment.objects.filter(workspace=workspace).select_related(
                    "social_account", "winner"
                )[:30]
            ],
            "max_variants": experiments.MAX_VARIANTS,
        },
    )


@login_required
@require_permission("view_analytics")
def experiment_detail(request: HttpRequest, workspace_id, experiment_id) -> HttpResponse:
    experiment = get_object_or_404(
        CaptionExperiment.objects.select_related("social_account", "winner"),
        id=experiment_id,
        workspace=request.workspace,
    )
    if request.method == "POST":
        if not request.workspace_membership.effective_permissions.get("create_posts"):
            messages.error(request, "You don't have permission to change tests.")
        elif request.POST.get("action") == "evaluate":
            if experiments.evaluate(experiment, force=True):
                messages.success(request, "Results calculated.")
            else:
                messages.info(request, "Every variant has to be published before results can be calculated.")
        elif request.POST.get("action") == "cancel":
            experiment.status = CaptionExperiment.Status.CANCELLED
            experiment.save(update_fields=["status"])
        return redirect("growth:experiment_detail", workspace_id=workspace_id, experiment_id=experiment.id)
    progress = experiments.experiment_progress(experiment)
    best_score = max((row["variant"].score or 0 for row in progress["rows"]), default=0)
    return _render(
        request,
        "growth/experiment_detail.html",
        "experiments",
        {"experiment": experiment, "progress": progress, "best_score": best_score},
    )


# --- auto-plugs -------------------------------------------------------------------------


@login_required
@require_permission("view_analytics")
def plugs(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    if request.method == "POST":
        if not request.workspace_membership.effective_permissions.get("create_posts"):
            messages.error(request, "You don't have permission to add plugs.")
            return redirect("growth:plugs", workspace_id=workspace.id)
        pp = get_object_or_404(PlatformPost, id=request.POST.get("platform_post"), post__workspace=workspace)
        text = request.POST.get("text", "").strip()[:2000]
        if not text:
            messages.error(request, "Write the plug text.")
        else:
            AutoPlug.objects.update_or_create(
                platform_post=pp,
                defaults={
                    "text": text,
                    "threshold": _int(request.POST.get("threshold"), 20, low=0, high=1_000_000),
                    "status": AutoPlug.Status.PENDING,
                    "error": "",
                },
            )
            messages.success(request, "Plug saved. It posts once the threshold is reached.")
        return redirect("growth:plugs", workspace_id=workspace.id)

    recent = (
        PlatformPost.objects.filter(
            post__workspace=workspace,
            status=PlatformPost.Status.PUBLISHED,
            published_at__gte=timezone.now() - autoplug.PLUG_WINDOW,
        )
        .select_related("post", "social_account")
        .order_by("-published_at")[:50]
    )
    return _render(
        request,
        "growth/plugs.html",
        "plugs",
        {
            "settings": GrowthSettings.for_workspace(workspace),
            "recent": recent,
            "plugs": AutoPlug.objects.filter(platform_post__post__workspace=workspace).select_related(
                "platform_post__post", "platform_post__social_account"
            )[:50],
        },
    )


@login_required
@require_permission("create_posts")
@require_POST
def plug_delete(request: HttpRequest, workspace_id, plug_id) -> HttpResponse:
    get_object_or_404(AutoPlug, id=plug_id, platform_post__post__workspace=request.workspace).delete()
    return redirect("growth:plugs", workspace_id=workspace_id)


# --- audience ---------------------------------------------------------------------------


@login_required
@require_permission("use_inbox")
def audience_view(request: HttpRequest, workspace_id) -> HttpResponse:
    workspace = request.workspace
    if request.method == "POST":
        platform = request.POST.get("platform", "")[:30]
        handle = request.POST.get("handle", "")[:255]
        if platform and handle:
            AudienceContact.objects.update_or_create(
                workspace=workspace,
                platform=platform,
                handle=handle,
                defaults={
                    "tags": parse_and_truncate_tag_string(request.POST.get("tags", "")),
                    "notes": request.POST.get("notes", "")[:5000],
                },
            )
            messages.success(request, f"Saved notes for {handle}.")
        return redirect(f"{request.path}?tag={request.GET.get('tag', '')}")

    days = _int(request.GET.get("days"), 90, low=7, high=365)
    tag = request.GET.get("tag", "").strip()
    all_tags = sorted(
        {t for tags in AudienceContact.objects.filter(workspace=workspace).values_list("tags", flat=True) for t in tags}
    )
    return _render(
        request,
        "growth/audience.html",
        "audience",
        {
            "engagers": audience.top_engagers(workspace, days=days, tag=tag),
            "days": days,
            "tag": tag,
            "all_tags": all_tags,
        },
    )


# --- settings ---------------------------------------------------------------------------


@login_required
@require_permission("manage_workspace_settings")
def settings_view(request: HttpRequest, workspace_id) -> HttpResponse:
    growth_settings = GrowthSettings.for_workspace(request.workspace)
    if request.method == "POST":
        was_plugging = growth_settings.autoplug_enabled
        growth_settings.utm_enabled = request.POST.get("utm_enabled") == "on"
        growth_settings.utm_source = request.POST.get("utm_source", "").strip()[:100] or "{platform}"
        growth_settings.utm_medium = request.POST.get("utm_medium", "").strip()[:100] or "social"
        growth_settings.utm_campaign = request.POST.get("utm_campaign", "").strip()[:100]
        growth_settings.recycle_enabled = request.POST.get("recycle_enabled") == "on"
        growth_settings.recycle_min_age_days = _int(request.POST.get("recycle_min_age_days"), 30, low=1, high=3650)
        growth_settings.recycle_gap_days = _int(request.POST.get("recycle_gap_days"), 60, low=1, high=3650)
        growth_settings.recycle_per_week = _int(request.POST.get("recycle_per_week"), 2, low=1, high=7)
        growth_settings.recycle_top_percent = _int(request.POST.get("recycle_top_percent"), 20, low=1, high=100)
        growth_settings.recycle_auto_schedule = request.POST.get("recycle_auto_schedule") == "on"
        growth_settings.recycle_ai_rewrite = request.POST.get("recycle_ai_rewrite") == "on"
        growth_settings.autoplug_enabled = request.POST.get("autoplug_enabled") == "on"
        growth_settings.autoplug_text = request.POST.get("autoplug_text", "").strip()[:2000]
        growth_settings.autoplug_threshold = _int(request.POST.get("autoplug_threshold"), 20, low=0, high=1_000_000)
        if growth_settings.autoplug_enabled and not was_plugging:
            growth_settings.autoplug_enabled_at = timezone.now()
        growth_settings.save()
        messages.success(request, "Growth settings saved.")
        return redirect("growth:settings", workspace_id=workspace_id)
    return _render(request, "growth/settings.html", "settings", {"settings": growth_settings})
