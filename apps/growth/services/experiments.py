"""Caption A/B tests.

Social networks don't split an audience for you, so a variant here is a
separate post of the same idea on the same account at a different time, and
the comparison is engagement per view where the platform reports views (the
same rate the analytics page uses), raw engagements otherwise. Timing and
news still add noise, which the result page says plainly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.composer.models import PlatformPost
from apps.composer.services import create_post

from .analysis import _denominator, engagement_total, score_posts

logger = logging.getLogger(__name__)

LABELS = ["A", "B", "C", "D"]
MAX_VARIANTS = len(LABELS)
#: A winner must beat the runner-up by this much; otherwise it's a tie.
MIN_WINNING_MARGIN = 0.10


class ExperimentError(Exception):
    pass


def create_experiment(
    *,
    workspace,
    social_account,
    name: str,
    captions: list[str],
    start_at: datetime,
    spacing_hours: int,
    evaluate_after_hours: int = 48,
    schedule: bool = True,
    author=None,
):
    from apps.growth.models import CaptionExperiment, CaptionVariant

    captions = [c.strip() for c in captions if c and c.strip()]
    if len(captions) < 2:
        raise ExperimentError("Add at least two captions to compare.")
    if len(captions) > MAX_VARIANTS:
        raise ExperimentError(f"Compare at most {MAX_VARIANTS} captions at a time.")
    if social_account.workspace_id != workspace.id:
        raise ExperimentError("That account isn't in this workspace.")
    if schedule and start_at <= timezone.now():
        raise ExperimentError("Pick a start time in the future.")

    with transaction.atomic():
        experiment = CaptionExperiment.objects.create(
            workspace=workspace,
            social_account=social_account,
            name=name.strip()[:200] or "Caption test",
            evaluate_after_hours=evaluate_after_hours,
            created_by=author,
        )
        for index, caption in enumerate(captions):
            when = start_at + timedelta(hours=spacing_hours * index)
            post = create_post(
                workspace=workspace,
                social_account=social_account,
                caption=caption,
                title=f"{experiment.name} — variant {LABELS[index]}"[:255],
                internal_notes=f"A/B test “{experiment.name}”, variant {LABELS[index]}.",
                scheduled_at=when if schedule else None,
                proposed_publish_at=None if schedule else when,
                status="scheduled" if schedule else "draft",
                author=author,
            )
            CaptionVariant.objects.create(experiment=experiment, label=LABELS[index], caption=caption, post=post)
    return experiment


def _variant_platform_post(variant) -> PlatformPost | None:
    if variant.post_id is None:
        return None
    return (
        PlatformPost.objects.filter(post_id=variant.post_id, social_account_id=variant.experiment.social_account_id)
        .select_related("post", "social_account")
        .first()
    )


def experiment_progress(experiment) -> dict:
    """Where each variant is, and when results are due."""
    rows = []
    last_published = None
    all_published = True
    for variant in experiment.variants.all():
        pp = _variant_platform_post(variant)
        status = pp.status if pp else "deleted"
        if pp is None or pp.status != PlatformPost.Status.PUBLISHED or not pp.published_at:
            all_published = False
        elif last_published is None or pp.published_at > last_published:
            last_published = pp.published_at
        rows.append({"variant": variant, "platform_post": pp, "status": status})
    due_at = (
        last_published + timedelta(hours=experiment.evaluate_after_hours) if all_published and last_published else None
    )
    return {"rows": rows, "all_published": all_published, "due_at": due_at}


def evaluate(experiment, *, force: bool = False) -> bool:
    """Score the variants and pick a winner once results are due. Returns True if evaluated."""
    from apps.growth.models import CaptionExperiment

    if experiment.status != CaptionExperiment.Status.RUNNING:
        return False
    progress = experiment_progress(experiment)
    if not progress["all_published"]:
        return False
    if not force and (progress["due_at"] is None or timezone.now() < progress["due_at"]):
        return False

    pps = [row["platform_post"] for row in progress["rows"]]
    scored = {s.platform_post.id: s for s in score_posts(pps)}
    use_rate = bool(scored) and all(s.basis == "rate" for s in scored.values())

    results = []
    for row in progress["rows"]:
        variant, pp = row["variant"], row["platform_post"]
        s = scored.get(pp.id)
        stats = s.stats if s else {}
        engagements = engagement_total(stats)
        denom = _denominator(stats)
        if use_rate and denom > 0:
            variant.score = round(engagements / denom * 100, 3)
            variant.score_basis = "engagement_rate"
        else:
            variant.score = engagements
            variant.score_basis = "engagements"
        variant.metrics = stats
        variant.save(update_fields=["score", "score_basis", "metrics"])
        results.append(variant)

    results.sort(key=lambda v: v.score or 0, reverse=True)
    winner = None
    if len(results) >= 2 and (results[0].score or 0) > 0:
        runner_up = results[1].score or 0
        if runner_up == 0 or (results[0].score - runner_up) / runner_up >= MIN_WINNING_MARGIN:
            winner = results[0]

    experiment.winner = winner
    experiment.status = CaptionExperiment.Status.COMPLETED
    experiment.evaluated_at = timezone.now()
    experiment.save(update_fields=["winner", "status", "evaluated_at"])
    return True


def evaluate_due_experiments() -> int:
    from apps.growth.models import CaptionExperiment

    done = 0
    for experiment in CaptionExperiment.objects.filter(status=CaptionExperiment.Status.RUNNING).select_related(
        "social_account"
    ):
        try:
            done += int(evaluate(experiment))
        except Exception:
            logger.exception("Evaluating caption experiment %s failed", experiment.id)
    return done
