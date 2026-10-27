"""Top engagers: the people who comment on and mention the workspace most."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Count, Max
from django.utils import timezone


@dataclass
class Engager:
    platform: str
    handle: str
    name: str
    avatar_url: str
    interactions: int
    last_seen: datetime
    tags: list[str]
    notes: str


def top_engagers(workspace, *, days: int = 90, limit: int = 50, tag: str = "") -> list[Engager]:
    from apps.growth.models import AudienceContact
    from apps.inbox.models import InboxMessage

    since = timezone.now() - timedelta(days=days)
    rows = (
        InboxMessage.objects.filter(workspace=workspace, received_at__gte=since)
        .exclude(sender_handle="")
        .values("social_account__platform", "sender_handle")
        .annotate(interactions=Count("id"), last_seen=Max("received_at"), name=Max("sender_name"))
        .order_by("-interactions", "-last_seen")
    )
    contacts = {(c.platform, c.handle): c for c in AudienceContact.objects.filter(workspace=workspace)}
    avatars = dict(
        InboxMessage.objects.filter(workspace=workspace, received_at__gte=since)
        .exclude(sender_avatar_url="")
        .values_list("sender_handle", "sender_avatar_url")
    )

    out: list[Engager] = []
    for row in rows:
        key = (row["social_account__platform"], row["sender_handle"])
        contact = contacts.get(key)
        tags = contact.tags if contact else []
        if tag and tag not in tags:
            continue
        out.append(
            Engager(
                platform=key[0],
                handle=key[1],
                name=row["name"] or key[1],
                avatar_url=avatars.get(key[1], ""),
                interactions=row["interactions"],
                last_seen=row["last_seen"],
                tags=tags,
                notes=contact.notes if contact else "",
            )
        )
        if len(out) >= limit:
            break
    return out
