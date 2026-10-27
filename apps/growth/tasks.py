"""Recurring growth jobs: auto-plugs, A/B evaluation, competitor sync, recycling."""

from __future__ import annotations

import logging
from datetime import timedelta

from background_task import background
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# Auto-plugs wait on analytics, which sync hourly, so polling faster gains nothing.
GROWTH_CYCLE_INTERVAL_SECONDS = 15 * 60
COMPETITORS_PER_CYCLE = 10


@background(schedule=0)
def run_growth_cycle():
    """One pass over every growth job.

    Each step is isolated and exceptions are swallowed: django-background-tasks
    backs a raising task off and eventually deletes it, and losing the schedule
    over one bad row would stop every workspace's automation.
    """
    for step in (_process_plugs, _evaluate_experiments, _sync_competitors, _recycle):
        try:
            step()
        except Exception:
            logger.exception("Growth cycle step %s failed", step.__name__)


def _process_plugs():
    from apps.growth.services.autoplug import process_pending_plugs

    process_pending_plugs()


def _evaluate_experiments():
    from apps.growth.services.experiments import evaluate_due_experiments

    evaluate_due_experiments()


def _sync_competitors():
    from apps.growth.models import Competitor
    from apps.growth.services.competitors import SYNC_INTERVAL_HOURS, sync_competitor

    stale = timezone.now() - timedelta(hours=SYNC_INTERVAL_HOURS)
    due = Competitor.objects.filter(Q(last_synced_at__isnull=True) | Q(last_synced_at__lt=stale)).order_by(
        F("last_synced_at").asc(nulls_first=True)  # never-synced (just added) first
    )
    # Bounded so one cycle stays short; the general worker lane is shared with other jobs.
    for competitor in due[:COMPETITORS_PER_CYCLE]:
        try:
            sync_competitor(competitor)
        except Exception:
            logger.exception("Syncing competitor %s failed", competitor.id)


def _recycle():
    from apps.growth.models import GrowthSettings
    from apps.growth.services.recycle import run_recycling

    for growth_settings in GrowthSettings.objects.filter(recycle_enabled=True).select_related("workspace"):
        try:
            run_recycling(growth_settings.workspace, growth_settings)
        except Exception:
            logger.exception("Recycling for workspace %s failed", growth_settings.workspace_id)
