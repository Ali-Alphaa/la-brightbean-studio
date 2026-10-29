"""Growth-marketing features layered on top of publishing and analytics.

Nothing here changes how a post is published unless a workspace opts in:
UTM tagging, recycling and default auto-plugs are all off by default.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class GrowthSettings(models.Model):
    """Per-workspace switches for the automated growth features."""

    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="growth_settings",
    )

    # UTM tagging, applied to links at publish time.
    utm_enabled = models.BooleanField(default=False)
    # ``{platform}`` is replaced with the account's platform key (e.g. "linkedin_company").
    utm_source = models.CharField(max_length=100, default="{platform}")
    utm_medium = models.CharField(max_length=100, default="social")
    utm_campaign = models.CharField(max_length=100, blank=True, default="")

    # Evergreen recycling of top performers.
    recycle_enabled = models.BooleanField(default=False)
    recycle_min_age_days = models.PositiveIntegerField(default=30)
    recycle_gap_days = models.PositiveIntegerField(
        default=60, help_text="Minimum days before the same post is recycled again."
    )
    recycle_per_week = models.PositiveIntegerField(default=2)
    recycle_top_percent = models.PositiveIntegerField(
        default=20, help_text="Only posts in this top percentage of their account are recycled."
    )
    recycle_auto_schedule = models.BooleanField(
        default=False, help_text="Schedule copies into the next free slot instead of leaving them as drafts."
    )
    recycle_ai_rewrite = models.BooleanField(default=False)
    recycle_last_run_at = models.DateTimeField(null=True, blank=True)

    # Default auto-plug attached to every newly published post.
    autoplug_enabled = models.BooleanField(default=False)
    autoplug_text = models.TextField(blank=True, default="")
    autoplug_threshold = models.PositiveIntegerField(default=20, help_text="Engagements before the plug is posted.")
    # Only posts published after the default plug was switched on get it.
    autoplug_enabled_at = models.DateTimeField(null=True, blank=True)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_settings"

    def __str__(self) -> str:
        return f"GrowthSettings({self.workspace_id})"

    @classmethod
    def for_workspace(cls, workspace) -> GrowthSettings:
        obj, _ = cls.objects.get_or_create(workspace=workspace)
        return obj


class RecycledPost(models.Model):
    """One recycle: ``new_post`` is a copy of ``source_post``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="recycled_posts")
    source_post = models.ForeignKey("composer.Post", on_delete=models.CASCADE, related_name="recycles")
    new_post = models.OneToOneField(
        "composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="recycled_from"
    )
    source_score = models.FloatField(default=0.0)
    automatic = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_recycled_post"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "created_at"])]


class AutoPlug(models.Model):
    """Comment posted on a live post once it crosses an engagement threshold."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for threshold"
        POSTED = "posted", "Posted"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    platform_post = models.OneToOneField("composer.PlatformPost", on_delete=models.CASCADE, related_name="auto_plug")
    text = models.TextField()
    threshold = models.PositiveIntegerField(default=20)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    comment_id = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")
    last_engagements = models.FloatField(default=0.0)
    posted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_auto_plug"
        ordering = ["-created_at"]


class Competitor(models.Model):
    """A public account the workspace tracks (not connected, read-only)."""

    class Platform(models.TextChoices):
        BLUESKY = "bluesky", "Bluesky"
        MASTODON = "mastodon", "Mastodon"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="competitors")
    platform = models.CharField(max_length=20, choices=Platform.choices)
    handle = models.CharField(max_length=255)
    display_name = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(max_length=500, blank=True, default="")
    profile_url = models.URLField(max_length=500, blank=True, default="")
    followers = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_competitor"
        ordering = ["display_name", "handle"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "platform", "handle"], name="growth_competitor_unique")
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.handle}"


class CompetitorSnapshot(models.Model):
    """Daily follower/post totals, for growth deltas."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="snapshots")
    date = models.DateField()
    followers = models.PositiveIntegerField(default=0)
    posts_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "growth_competitor_snapshot"
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["competitor", "date"], name="growth_competitor_snapshot_unique")]


class CompetitorPost(models.Model):
    """A recent public post by a competitor; the inspiration library."""

    competitor = models.ForeignKey(Competitor, on_delete=models.CASCADE, related_name="posts")
    external_id = models.CharField(max_length=255)
    url = models.URLField(max_length=500, blank=True, default="")
    text = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(db_index=True)
    likes = models.PositiveIntegerField(default=0)
    replies = models.PositiveIntegerField(default=0)
    reposts = models.PositiveIntegerField(default=0)
    engagements = models.PositiveIntegerField(default=0, db_index=True)
    captured_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_competitor_post"
        ordering = ["-engagements"]
        constraints = [
            models.UniqueConstraint(fields=["competitor", "external_id"], name="growth_competitor_post_unique")
        ]


class CaptionExperiment(models.Model):
    """A/B test: the same idea published with different captions at different times."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="caption_experiments")
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="caption_experiments"
    )
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING, db_index=True)
    evaluate_after_hours = models.PositiveIntegerField(default=48)
    winner = models.ForeignKey(
        "growth.CaptionVariant", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "growth_caption_experiment"
        ordering = ["-created_at"]


class CaptionVariant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    experiment = models.ForeignKey(CaptionExperiment, on_delete=models.CASCADE, related_name="variants")
    label = models.CharField(max_length=4)
    caption = models.TextField()
    post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    score = models.FloatField(null=True, blank=True)
    score_basis = models.CharField(max_length=30, blank=True, default="")
    metrics = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "growth_caption_variant"
        ordering = ["label"]


class AudienceContact(models.Model):
    """CRM notes on a person who engages with the workspace's accounts.

    Engagement counts are computed from the inbox on read; this row only holds
    what the team adds (tags, notes), so it exists only once someone edits it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="audience_contacts")
    platform = models.CharField(max_length=30)
    handle = models.CharField(max_length=255)
    tags = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")
    #: Last automated touch (e.g. a comment → DM rule firing); null for hand-edited rows.
    last_interaction_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_audience_contact"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "platform", "handle"], name="growth_contact_unique")
        ]


# --- Instagram + TikTok growth: inspiration library, voices, repurpose, comment → DM ------------


class SocialPlatform(models.TextChoices):
    """The two networks the inspiration/voice/repurpose features cover."""

    INSTAGRAM = "instagram", "Instagram"
    TIKTOK = "tiktok", "TikTok"


class InspirationPost(models.Model):
    """A public Instagram/TikTok post saved because it outperformed its author.

    Rows arrive from the browser extension (auto-capture or a manual save),
    from "Save by link", or from Instagram Business Discovery.
    """

    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        CAROUSEL = "carousel", "Carousel"

    class Source(models.TextChoices):
        AUTO = "auto", "Auto-captured"
        MANUAL = "manual", "Saved manually"
        LINK = "link", "Saved by link"
        DISCOVERY = "discovery", "Business Discovery"

    HOOK_TYPES = ["question", "contrarian", "story", "list", "stat", "how-to", "announcement", "other"]
    FORMATS = ["one-liner", "carousel-tips", "tutorial", "talking-head", "story", "meme", "other"]

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="inspiration_posts")
    platform = models.CharField(max_length=20, choices=SocialPlatform.choices)
    external_id = models.CharField(max_length=255)
    url = models.URLField(max_length=500, blank=True, default="")
    author_handle = models.CharField(max_length=255, blank=True, default="")
    author_name = models.CharField(max_length=255, blank=True, default="")
    caption = models.TextField(blank=True, default="")
    likes = models.PositiveBigIntegerField(null=True, blank=True)
    comments = models.PositiveBigIntegerField(null=True, blank=True)
    shares = models.PositiveBigIntegerField(null=True, blank=True)
    views = models.PositiveBigIntegerField(null=True, blank=True)
    saves = models.PositiveBigIntegerField(null=True, blank=True)
    media_type = models.CharField(max_length=10, choices=MediaType.choices, blank=True, default="")
    posted_at = models.DateTimeField(null=True, blank=True)
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)
    source = models.CharField(max_length=10, choices=Source.choices, default=Source.AUTO)
    #: likes ÷ the author's median likes when the post was saved ("4.2× usual").
    ratio = models.FloatField(null=True, blank=True)
    topics = models.JSONField(default=list, blank=True)
    hook_type = models.CharField(max_length=20, blank=True, default="")
    format = models.CharField(max_length=20, blank=True, default="")
    on_niche = models.BooleanField(null=True, blank=True)
    analyzed_at = models.DateTimeField(null=True, blank=True)
    hidden = models.BooleanField(default=False)

    class Meta:
        db_table = "growth_inspiration_post"
        ordering = ["-captured_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "platform", "external_id"], name="growth_inspiration_post_unique"
            )
        ]
        indexes = [models.Index(fields=["workspace", "analyzed_at"], name="growth_insp_ws_analyzed")]

    def __str__(self) -> str:
        return f"{self.platform}:{self.external_id}"


class AuthorBaseline(models.Model):
    """What an author's posts usually get, learned from the posts seen so far.

    ``samples`` holds ``{"id", "likes"}`` for the last 30 distinct posts.
    ``pending`` holds up to 10 captured items that couldn't be judged yet
    (too few samples); they are re-checked whenever a new sample arrives.
    """

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="author_baselines")
    platform = models.CharField(max_length=20, choices=SocialPlatform.choices)
    handle = models.CharField(max_length=255)
    samples = models.JSONField(default=list, blank=True)
    pending = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_author_baseline"
        constraints = [
            models.UniqueConstraint(fields=["workspace", "platform", "handle"], name="growth_author_baseline_unique")
        ]

    def __str__(self) -> str:
        return f"{self.platform}:{self.handle}"


class GrowthVoice(models.Model):
    """How one of the workspace's own accounts writes: sample posts + a style guide."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="growth_voices")
    platform = models.CharField(max_length=20, choices=SocialPlatform.choices)
    handle = models.CharField(max_length=255)
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.SET_NULL, null=True, blank=True, related_name="growth_voices"
    )
    #: ``{"id", "text", "likes", "comments", "views", "posted_at"}`` per own post, newest first, capped.
    samples = models.JSONField(default=list, blank=True)
    style_guide = models.TextField(blank=True, default="")
    style_guide_generated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "growth_voice"
        ordering = ["platform", "handle"]
        constraints = [models.UniqueConstraint(fields=["workspace", "platform", "handle"], name="growth_voice_unique")]

    def __str__(self) -> str:
        return f"{self.platform}:@{self.handle}"


class RepurposeRun(models.Model):
    """One "Repurpose" click: the sources, the 5 variations, and what was picked."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="repurpose_runs")
    target_platform = models.CharField(max_length=20, choices=SocialPlatform.choices)
    source_ids = models.JSONField(default=list, blank=True)
    voice = models.ForeignKey(GrowthVoice, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    variations = models.JSONField(default=list, blank=True)
    chosen_index = models.PositiveSmallIntegerField(null=True, blank=True)
    post = models.ForeignKey("composer.Post", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_repurpose_run"
        ordering = ["-created_at"]


class CommentDMRule(models.Model):
    """Instagram: when a comment contains a keyword, DM the commenter a link."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="comment_dm_rules")
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount", on_delete=models.CASCADE, related_name="comment_dm_rules"
    )
    #: Instagram media id; empty means every post on the account.
    media_id = models.CharField(max_length=255, blank=True, default="")
    name = models.CharField(max_length=200, blank=True, default="")
    keywords = models.JSONField(default=list, blank=True)
    dm_text = models.TextField()
    public_reply_text = models.TextField(blank=True, default="")
    tag = models.CharField(max_length=50, blank=True, default="")
    enabled = models.BooleanField(default=True)
    daily_cap = models.PositiveIntegerField(default=200)
    once_per_user = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "growth_comment_dm_rule"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.name or ", ".join(self.keywords)


class CommentDMLog(models.Model):
    """One comment a rule matched. ``comment_id`` is unique: a comment is answered at most once."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"
        SIMULATED = "simulated", "Simulated"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(CommentDMRule, on_delete=models.CASCADE, related_name="logs")
    comment_id = models.CharField(max_length=255, unique=True)
    commenter_id = models.CharField(max_length=255, blank=True, default="")
    commenter_handle = models.CharField(max_length=255, blank=True, default="")
    comment_text = models.TextField(blank=True, default="")
    media_id = models.CharField(max_length=255, blank=True, default="")
    keyword = models.CharField(max_length=100, blank=True, default="")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    dm_message_id = models.CharField(max_length=255, blank=True, default="")
    public_reply_id = models.CharField(max_length=255, blank=True, default="")
    error = models.TextField(blank=True, default="")
    commented_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "growth_comment_dm_log"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["rule", "created_at"], name="growth_cdm_log_rule_created")]
