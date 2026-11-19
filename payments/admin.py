from django.contrib import admin

from .models import (ApiClient, MerchantAccount, PaymentAttempt, PaymentIntent, ProviderEvent,
                     ReconciliationException, SimTransaction, SimWallet)


@admin.register(MerchantAccount)
class MerchantAccountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "client", "party_ref", "provider", "merchant_code",
                    "settlement_mode", "status", "is_active")
    list_filter = ("client", "provider", "settlement_mode", "status", "is_active")
    search_fields = ("party_ref", "merchant_code", "display_name")
    exclude = ("credentials_encrypted",)  # set credentials in code, never in a form


class AttemptInline(admin.TabularInline):
    model = PaymentAttempt
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in PaymentAttempt._meta.fields]


@admin.register(PaymentIntent)
class PaymentIntentAdmin(admin.ModelAdmin):
    list_display = ("reference", "kind", "amount", "currency", "provider",
                    "settlement_mode", "settles_to", "status", "created_at")
    list_filter = ("status", "kind", "provider", "settlement_mode")
    search_fields = ("reference", "source_ref", "payer_ref", "payee_ref", "idempotency_key")
    readonly_fields = [f.name for f in PaymentIntent._meta.fields]
    inlines = [AttemptInline]

    def has_add_permission(self, request):
        return False


@admin.register(ProviderEvent)
class ProviderEventAdmin(admin.ModelAdmin):
    """Append-only: view, never edit or delete."""
    list_display = ("received_at", "provider", "source", "event_type", "attempt", "actor_ref")
    list_filter = ("provider", "source")
    readonly_fields = [f.name for f in ProviderEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReconciliationException)
class ReconciliationExceptionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "intent", "is_resolved")
    list_filter = ("kind", "is_resolved")
    readonly_fields = ("kind", "intent", "event", "detail", "created_at")


admin.site.register(SimWallet)
admin.site.register(SimTransaction)


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    """Create clients with `manage.py create_api_client`; keys are never shown here."""
    list_display = ("name", "key_prefix", "is_active", "created_at", "last_used_at")
    readonly_fields = ("key_prefix", "key_hash", "created_at", "last_used_at")

    def has_add_permission(self, request):
        return False
