from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "payments"
    verbose_name = "Payments"

    def ready(self):
        # Connects merchant verification to the payment signals.
        from . import merchants  # noqa: F401
