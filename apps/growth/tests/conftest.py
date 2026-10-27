from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from apps.social_accounts.models import SocialAccount


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Growth WS", organization=organization, timezone="UTC")


@pytest.fixture
def account(workspace):
    # Bluesky reports likes/replies/reposts but no views, so posts score by count.
    return SocialAccount.objects.create(
        workspace=workspace,
        platform="bluesky",
        account_platform_id="did:plc:me",
        account_name="Me on Bluesky",
        oauth_access_token="token",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def make_post(account):
    """Create a published post with the given engagement, ``days_ago`` at ``hour`` UTC."""
    from apps.analytics.models import PostInsightsSnapshot
    from apps.composer.models import PlatformPost, Post

    counter = {"n": 0}

    def _make(caption="hello", *, likes=10, published_at=None, social_account=None, extra_stats=None):
        counter["n"] += 1
        social_account = social_account or account
        published_at = published_at or timezone.now() - timedelta(days=40)
        post = Post.objects.create(workspace=social_account.workspace, caption=caption, published_at=published_at)
        pp = PlatformPost.objects.create(
            post=post,
            social_account=social_account,
            status=PlatformPost.Status.PUBLISHED,
            published_at=published_at,
            platform_post_id=f"at://post/{counter['n']}",
        )
        stats = {"likes": likes, **(extra_stats or {})}
        for key, value in stats.items():
            PostInsightsSnapshot.objects.create(platform_post=pp, metric_key=key, date=published_at.date(), value=value)
        return pp

    return _make


def at(weekday: int, hour: int, weeks_ago: int = 2) -> datetime:
    """An aware UTC datetime on ``weekday`` (0=Mon) at ``hour`` some weeks back."""
    now = timezone.now()
    base = (now - timedelta(weeks=weeks_ago)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return base - timedelta(days=(base.weekday() - weekday) % 7)
