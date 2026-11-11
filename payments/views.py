"""Webhook endpoint: /payments/webhooks/<provider>/"""
import logging

from django.http import HttpResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .exceptions import CapabilityNotSupported, PaymentError, WebhookVerificationFailed
from .services import PaymentService

log = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def provider_webhook(request, provider):
    headers = {k: v for k, v in request.headers.items()}
    try:
        PaymentService.handle_webhook(provider, headers, request.body)
    except WebhookVerificationFailed:
        log.warning("Rejected %s webhook: bad signature", provider)
        return HttpResponse(status=401)
    except CapabilityNotSupported:
        return HttpResponse(status=404)
    except PaymentError as exc:
        log.exception("Webhook from %s could not be applied", provider)
        return HttpResponseBadRequest(str(exc))
    # 200 even for duplicates/unmatched: the event is stored and the provider
    # should stop retrying.
    return HttpResponse("ok")
