from django.urls import path

from . import views
from . import views_inspiration as ig

app_name = "growth"

urlpatterns = [
    path("", views.index, name="index"),
    path("best-times/", views.best_times, name="best_times"),
    path("best-times/apply/", views.apply_best_times, name="apply_best_times"),
    path("hashtags/", views.hashtags, name="hashtags"),
    path("top-posts/", views.top_posts, name="top_posts"),
    path("top-posts/<uuid:post_id>/recycle/", views.recycle_now, name="recycle_now"),
    path("competitors/", views.competitor_list, name="competitors"),
    path("competitors/<uuid:competitor_id>/refresh/", views.competitor_refresh, name="competitor_refresh"),
    path("competitors/<uuid:competitor_id>/delete/", views.competitor_delete, name="competitor_delete"),
    path("inspiration/<int:post_id>/idea/", views.inspiration_to_idea, name="inspiration_to_idea"),
    path("experiments/", views.experiment_list, name="experiments"),
    path("experiments/<uuid:experiment_id>/", views.experiment_detail, name="experiment_detail"),
    path("plugs/", views.plugs, name="plugs"),
    path("plugs/<uuid:plug_id>/delete/", views.plug_delete, name="plug_delete"),
    path("audience/", views.audience_view, name="audience"),
    path("settings/", views.settings_view, name="settings"),
    # Instagram + TikTok
    path("library/", ig.inspiration_library, name="inspiration"),
    path("library/add-link/", ig.inspiration_add_link, name="inspiration_add_link"),
    path("library/analyze/", ig.inspiration_analyze, name="inspiration_analyze"),
    path("library/discover/", ig.inspiration_discover, name="inspiration_discover"),
    path("library/discover/save/", ig.inspiration_discover_save, name="inspiration_discover_save"),
    path("library/repurpose/", ig.inspiration_repurpose, name="inspiration_repurpose"),
    path("library/<int:post_id>/hide/", ig.inspiration_hide, name="inspiration_hide"),
    path("library/<int:post_id>/rewrite/", ig.inspiration_rewrite, name="inspiration_rewrite"),
    path("repurpose/<uuid:run_id>/", ig.repurpose_detail, name="repurpose_detail"),
    path("repurpose/<uuid:run_id>/choose/", ig.repurpose_choose, name="repurpose_choose"),
    path("voices/", ig.voices, name="voices"),
    path("voices/<int:voice_id>/save/", ig.voice_save, name="voice_save"),
    path("voices/<int:voice_id>/regenerate/", ig.voice_regenerate, name="voice_regenerate"),
    path("voices/<int:voice_id>/pull/", ig.voice_pull, name="voice_pull"),
    path("comment-dm/", ig.comment_dm_rules, name="comment_dm"),
    path("comment-dm/<uuid:rule_id>/toggle/", ig.comment_dm_toggle, name="comment_dm_toggle"),
    path("comment-dm/<uuid:rule_id>/simulate/", ig.comment_dm_simulate, name="comment_dm_simulate"),
]
