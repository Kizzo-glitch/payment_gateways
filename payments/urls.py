from django.urls import path

from . import api, views

app_name = "payments"

urlpatterns = [
    path("webhooks/<slug:provider>/", views.provider_webhook, name="webhook"),
]

# Client API, version 1. Mounted at /api/v1/ by the project urls.
api_v1 = ([
    path("health/", api.health, name="health"),
    path("merchants/", api.merchants_collection, name="merchants"),
    path("merchants/<int:merchant_id>/", api.merchant_detail, name="merchant"),
    path("merchants/<int:merchant_id>/verify/", api.merchant_verify, name="merchant-verify"),
    path("merchants/<int:merchant_id>/verify/confirm/", api.merchant_verify_confirm,
         name="merchant-verify-confirm"),
    path("merchants/<int:merchant_id>/disable/", api.merchant_disable, name="merchant-disable"),
    path("collections/", api.collections_collection, name="collections"),
    path("collections/<uuid:collection_id>/", api.collection_detail, name="collection"),
    path("collections/<uuid:collection_id>/otp/", api.collection_otp, name="collection-otp"),
], "api")
