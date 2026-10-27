from django.apps import AppConfig


class GrowthConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.growth"
    verbose_name = "Growth"

    def ready(self):
        from django.db.models.signals import post_migrate

        post_migrate.connect(self._register_tasks, sender=self)

    @staticmethod
    def _register_tasks(sender, **kwargs):
        from apps.common.background import register_recurring_task
        from apps.growth.tasks import GROWTH_CYCLE_INTERVAL_SECONDS, run_growth_cycle

        register_recurring_task(
            run_growth_cycle,
            repeat=GROWTH_CYCLE_INTERVAL_SECONDS,
            verbose_name="run_growth_cycle",
        )
