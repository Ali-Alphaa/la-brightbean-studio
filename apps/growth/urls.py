from django.urls import path

from . import views

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
]
