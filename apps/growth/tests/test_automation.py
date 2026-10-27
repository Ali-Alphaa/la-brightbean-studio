from datetime import time, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import timezone

from apps.composer.models import PlatformPost
from apps.growth.models import AutoPlug, CaptionExperiment, GrowthSettings, RecycledPost
from apps.growth.services import autoplug, experiments, recycle


@pytest.fixture
def growth(workspace):
    return GrowthSettings.objects.create(workspace=workspace, recycle_enabled=True, recycle_top_percent=20)


# --- recycling -----------------------------------------------------------------


@pytest.mark.django_db
def test_recycle_picks_old_top_posts_and_respects_the_gap(workspace, make_post, growth):
    best = make_post("Evergreen thread", likes=100)
    make_post("Too new to recycle", likes=200, published_at=timezone.now() - timedelta(days=3))
    for _ in range(8):
        make_post(likes=10)

    candidates = recycle.recycle_candidates(workspace, growth)
    assert [c.platform_post.id for c in candidates] == [best.id]

    new_post, scheduled = recycle.recycle_post(best.post, growth, score=candidates[0].lift)
    assert scheduled == 0  # drafts by default
    assert new_post.caption == "Evergreen thread"
    assert new_post.platform_posts.get().status == "draft"
    assert RecycledPost.objects.filter(source_post=best.post, new_post=new_post).exists()

    # Within the gap neither the original nor the copy is offered again.
    assert recycle.recycle_candidates(workspace, growth) == []


@pytest.mark.django_db
def test_recycle_can_schedule_into_the_next_free_slot(account, make_post, growth):
    from apps.calendar.models import PostingSlot

    for day in range(7):
        PostingSlot.objects.create(social_account=account, day_of_week=day, time=time(9, 0))
    growth.recycle_auto_schedule = True
    growth.save()
    pp = make_post("Evergreen", likes=100)

    new_post, scheduled = recycle.recycle_post(pp.post, growth)

    child = new_post.platform_posts.get()
    assert scheduled == 1
    assert child.status == "scheduled"
    assert child.scheduled_at > timezone.now()


@pytest.mark.django_db
def test_run_recycling_honours_switch_daily_cadence_and_weekly_quota(workspace, make_post, growth):
    for likes in (100, 90, 80, 10, 10, 10, 10, 10, 10, 10):
        make_post(likes=likes)
    growth.recycle_top_percent = 30
    growth.recycle_per_week = 2
    growth.save()

    assert recycle.run_recycling(workspace, growth) == 1
    assert recycle.run_recycling(workspace, growth) == 0  # already ran today
    assert recycle.run_recycling(workspace, growth, force=True) == 1
    assert recycle.run_recycling(workspace, growth, force=True) == 0  # weekly quota of 2 used

    growth.recycle_enabled = False
    assert recycle.run_recycling(workspace, growth, force=True) == 0


# --- A/B experiments --------------------------------------------------------------


@pytest.mark.django_db
def test_create_experiment_schedules_spaced_variants(workspace, account):
    start = timezone.now() + timedelta(days=1)
    experiment = experiments.create_experiment(
        workspace=workspace,
        social_account=account,
        name="Hook test",
        captions=["Question hook?", "Bold claim.", " "],
        start_at=start,
        spacing_hours=24,
    )

    variants = list(experiment.variants.all())
    assert [v.label for v in variants] == ["A", "B"]
    times = [PlatformPost.objects.get(post=v.post).scheduled_at for v in variants]
    assert times[1] - times[0] == timedelta(hours=24)


@pytest.mark.django_db
def test_create_experiment_validation(workspace, account):
    with pytest.raises(experiments.ExperimentError):
        experiments.create_experiment(
            workspace=workspace,
            social_account=account,
            name="x",
            captions=["only one"],
            start_at=timezone.now() + timedelta(hours=1),
            spacing_hours=24,
        )
    with pytest.raises(experiments.ExperimentError):
        experiments.create_experiment(
            workspace=workspace,
            social_account=account,
            name="x",
            captions=["a", "b"],
            start_at=timezone.now() - timedelta(hours=1),
            spacing_hours=24,
        )


def _publish(variant, likes, hours_ago):
    from apps.analytics.models import PostInsightsSnapshot

    pp = PlatformPost.objects.get(post=variant.post)
    pp.status = PlatformPost.Status.PUBLISHED
    pp.published_at = timezone.now() - timedelta(hours=hours_ago)
    pp.platform_post_id = f"id-{variant.label}"
    pp.save()
    PostInsightsSnapshot.objects.create(platform_post=pp, metric_key="likes", date=pp.published_at.date(), value=likes)


@pytest.mark.django_db
def test_evaluate_waits_for_the_window_then_picks_a_clear_winner(workspace, account):
    experiment = experiments.create_experiment(
        workspace=workspace,
        social_account=account,
        name="Hook",
        captions=["A text", "B text"],
        start_at=timezone.now() + timedelta(hours=1),
        spacing_hours=24,
        evaluate_after_hours=48,
    )
    a, b = experiment.variants.all()
    _publish(a, likes=10, hours_ago=100)
    assert experiments.evaluate(experiment) is False  # B not published yet

    _publish(b, likes=30, hours_ago=10)
    assert experiments.evaluate(experiment) is False  # window not over
    assert experiments.evaluate(experiment, force=True) is True

    experiment.refresh_from_db()
    assert experiment.status == CaptionExperiment.Status.COMPLETED
    assert experiment.winner.label == "B"
    assert experiment.winner.score == 30


@pytest.mark.django_db
def test_evaluate_calls_a_near_tie_a_tie(workspace, account):
    experiment = experiments.create_experiment(
        workspace=workspace,
        social_account=account,
        name="Hook",
        captions=["A", "B"],
        start_at=timezone.now() + timedelta(hours=1),
        spacing_hours=1,
    )
    a, b = experiment.variants.all()
    _publish(a, likes=100, hours_ago=100)
    _publish(b, likes=105, hours_ago=99)

    assert experiments.evaluate(experiment) is True
    experiment.refresh_from_db()
    assert experiment.winner is None


# --- auto-plug -----------------------------------------------------------------------


@pytest.mark.django_db
def test_plug_waits_for_threshold_then_comments_once(make_post):
    pp = make_post("Viral post", likes=5, published_at=timezone.now() - timedelta(hours=2))
    plug = AutoPlug.objects.create(platform_post=pp, text="Join my newsletter https://ex.com", threshold=20)
    provider = mock.Mock()
    provider.publish_comment.return_value = SimpleNamespace(comment_id="c1")

    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")):
        assert autoplug.process_plug(plug) == AutoPlug.Status.PENDING
        provider.publish_comment.assert_not_called()

        pp.analytics_snapshots.filter(metric_key="likes").update(value=25)
        assert autoplug.process_plug(plug) == AutoPlug.Status.POSTED

    provider.publish_comment.assert_called_once_with(
        access_token="tok", post_id=pp.platform_post_id, text="Join my newsletter https://ex.com"
    )
    plug.refresh_from_db()
    assert plug.comment_id == "c1"
    assert plug.last_engagements == 25


@pytest.mark.django_db
def test_plug_expires_and_unsupported_platforms_fail_cleanly(make_post):
    old = make_post(likes=50, published_at=timezone.now() - timedelta(days=9))
    expired = AutoPlug.objects.create(platform_post=old, text="plug", threshold=1)
    assert autoplug.process_plug(expired) == AutoPlug.Status.EXPIRED

    recent = make_post(likes=50, published_at=timezone.now() - timedelta(hours=1))
    plug = AutoPlug.objects.create(platform_post=recent, text="plug", threshold=1)
    provider = mock.Mock()
    provider.publish_comment.side_effect = NotImplementedError
    with mock.patch("apps.publisher.engine._provider_and_access_token", return_value=(provider, "tok")):
        assert autoplug.process_plug(plug) == AutoPlug.Status.FAILED
    plug.refresh_from_db()
    assert "doesn't support comments" in plug.error


@pytest.mark.django_db
def test_default_plug_attaches_only_to_posts_after_it_was_enabled(workspace, make_post):
    before = make_post(published_at=timezone.now() - timedelta(hours=5))
    settings = GrowthSettings.objects.create(
        workspace=workspace,
        autoplug_enabled=True,
        autoplug_text="Subscribe!",
        autoplug_threshold=15,
        autoplug_enabled_at=timezone.now() - timedelta(hours=1),
    )
    after = make_post(published_at=timezone.now())

    assert autoplug.attach_default_plugs(settings) == 1
    assert not AutoPlug.objects.filter(platform_post=before).exists()
    assert AutoPlug.objects.get(platform_post=after).threshold == 15
    assert autoplug.attach_default_plugs(settings) == 0


@pytest.mark.django_db
def test_growth_cycle_survives_a_failing_step():
    from apps.growth import tasks

    def _process_plugs():
        raise RuntimeError("boom")

    calls = []
    with (
        mock.patch.object(tasks, "_process_plugs", _process_plugs),
        mock.patch.object(tasks, "_evaluate_experiments", lambda: calls.append("evaluated")),
    ):
        tasks.run_growth_cycle.now()
    assert calls == ["evaluated"]
