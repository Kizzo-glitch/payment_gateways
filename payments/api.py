"""
Gateway API, version 1 (JSON over HTTPS).

Authentication: every request except /health/ needs
    Authorization: Bearer <api key>
Keys come from `python manage.py create_api_client "<name>"`.

Errors always look like:
    {"error": {"code": "merchant_not_found", "message": "..."}}

Credentials are accepted on registration but never returned by any endpoint.
"""
import functools
import json
import logging

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from decimal import Decimal, InvalidOperation

from . import merchants
from .exceptions import (CapabilityNotSupported, IdempotencyConflict, MerchantNotConfigured,
                         MerchantNotVerified, NoRouteAvailable, PaymentError,
                         PlatformCustodyNotPermitted)
from .models import ApiClient, MerchantAccount, PaymentIntent, PaymentKind
from .providers.registry import all_adapters
from .providers.simulator import _SimulatorBase
from .services import PaymentService

log = logging.getLogger(__name__)
MAX_BODY_BYTES = 64 * 1024


def error(status, code, message):
    return JsonResponse({"error": {"code": code, "message": message}}, status=status)


def api_endpoint(*methods, auth=True):
    """Method check, API-key authentication, JSON parsing and error mapping."""
    def decorator(view):
        @csrf_exempt
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in methods:
                return error(405, "method_not_allowed", f"Use {' or '.join(methods)}.")
            if auth:
                header = request.headers.get("Authorization", "")
                key = header[7:].strip() if header.startswith("Bearer ") else ""
                client = ApiClient.authenticate(key)
                if client is None:
                    return error(401, "unauthorized", "Missing or invalid API key.")
                ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
                request.api_client = client
            request.json = {}
            if request.method == "POST" and request.body:
                if len(request.body) > MAX_BODY_BYTES:
                    return error(413, "too_large", "Request body is too large.")
                try:
                    request.json = json.loads(request.body)
                except (ValueError, UnicodeDecodeError):
                    return error(400, "invalid_json", "Body must be valid JSON.")
                if not isinstance(request.json, dict):
                    return error(400, "invalid_json", "Body must be a JSON object.")
            try:
                return view(request, *args, **kwargs)
            except merchants.MerchantError as exc:
                return error(400, exc.code, str(exc))
            except IdempotencyConflict as exc:
                return error(409, "idempotency_conflict", str(exc))
            except MerchantNotVerified as exc:
                return error(400, "merchant_not_verified", str(exc))
            except MerchantNotConfigured as exc:
                return error(400, "merchant_not_found", str(exc))
            except PlatformCustodyNotPermitted as exc:
                return error(400, "custody_not_permitted", str(exc))
            except CapabilityNotSupported as exc:
                return error(400, "not_supported_by_provider", str(exc))
            except NoRouteAvailable as exc:
                return error(400, "no_route", str(exc))
            except PaymentError as exc:
                return error(400, "payment_error", str(exc))
            except Exception:  # noqa: BLE001 — never leak internals to clients
                log.exception("API error in %s", view.__name__)
                return error(500, "server_error", "Something went wrong. Please try again.")
        return wrapper
    return decorator


def _get_merchant(request, merchant_id):
    return MerchantAccount.objects.filter(pk=merchant_id, client=request.api_client).first()


def _not_found():
    return error(404, "merchant_not_found", "No such merchant for this API key.")


def _iso(dt):
    return dt.isoformat() if dt else None


def merchant_json(m: MerchantAccount):
    data = {
        "id": m.pk,
        "party_ref": m.party_ref,
        "provider": m.provider,
        "mode": m.settlement_mode,
        "merchant_code": m.merchant_code,
        "display_name": m.display_name,
        "status": m.status,
        "verified_at": _iso(m.verified_at),
        "last_error": m.last_error,
        "created_at": _iso(m.created_at),
        "updated_at": _iso(m.updated_at),
        "verification": None,
    }
    intent = m.verification_intent
    if intent is not None:
        attempt = intent.current_attempt()
        data["verification"] = {
            "status": intent.status,
            "required_action": intent.required_action or None,
            "amount": str(intent.amount),
            "expires_at": _iso(attempt.expires_at) if attempt else None,
        }
    return data


# ---------------------------------------------------------------------------
@api_endpoint("GET", auth=False)
def health(request):
    return JsonResponse({"status": "ok"})


@api_endpoint("POST")
def merchants_collection(request):
    """Register (or re-register) a merchant. 201 if new, 200 if replaced."""
    body = request.json
    merchant, created = merchants.register(
        request.api_client,
        party_ref=body.get("party_ref"),
        provider=body.get("provider"),
        mode=body.get("mode", "direct"),
        merchant_code=body.get("merchant_code"),
        display_name=body.get("display_name", ""),
        credentials=body.get("credentials") or {},
    )
    return JsonResponse(merchant_json(merchant), status=201 if created else 200)


@api_endpoint("GET")
def merchant_detail(request, merchant_id):
    m = _get_merchant(request, merchant_id)
    return JsonResponse(merchant_json(m)) if m else _not_found()


@api_endpoint("POST")
def merchant_verify(request, merchant_id):
    """Start the M1.00 test payment. The owner gets an OTP on `msisdn`."""
    m = _get_merchant(request, merchant_id)
    if m is None:
        return _not_found()
    intent = merchants.start_verification(m, request.json.get("msisdn"))
    m.refresh_from_db()
    data = merchant_json(m)
    # Simulator only: hand back the OTP a real provider would SMS, for testing.
    if isinstance(merchants.get_adapter(m.provider), _SimulatorBase):
        attempt = intent.current_attempt()
        event = attempt.events.order_by("-received_at").first() if attempt else None
        otp = (((event.payload or {}).get("raw") or {}).get("simulated_otp")
               if event else None)
        if otp:
            data["test_otp"] = otp
    return JsonResponse(data, status=202)


@api_endpoint("POST")
def merchant_verify_confirm(request, merchant_id):
    m = _get_merchant(request, merchant_id)
    if m is None:
        return _not_found()
    m = merchants.confirm_verification(m, request.json.get("otp"))
    return JsonResponse(merchant_json(m))


@api_endpoint("POST")
def merchant_disable(request, merchant_id):
    m = _get_merchant(request, merchant_id)
    if m is None:
        return _not_found()
    return JsonResponse(merchant_json(merchants.disable(m)))


# ---------------------------------------------------------------------------
# Collections: a payer (borrower) pays a verified merchant (lender).
# ---------------------------------------------------------------------------
def _idem(request, key):
    """Idempotency keys are per API client, so two clients can't collide."""
    return f"c{request.api_client.pk}:{key}"


def _simulated_otp(intent):
    if not isinstance(merchants.get_adapter(intent.provider), _SimulatorBase):
        return None
    attempt = intent.current_attempt()
    event = attempt.events.order_by("-received_at").first() if attempt else None
    raw = ((event.payload or {}).get("raw") or {}) if event else {}
    return raw.get("simulated_otp")


def collection_json(intent: PaymentIntent, request=None):
    attempt = intent.current_attempt()
    prefix = f"c{intent.client_id}:"
    key = intent.idempotency_key
    return {
        "id": str(intent.id),
        "status": intent.status,
        "required_action": intent.required_action or None,
        "amount": str(intent.amount),
        "currency": intent.currency,
        "reference": intent.reference,
        "source_ref": intent.source_ref,
        "payer_ref": intent.payer_ref,
        "payee_ref": intent.payee_ref,
        "provider": intent.provider,
        "settles_to": intent.settles_to,
        "idempotency_key": key[len(prefix):] if key.startswith(prefix) else key,
        "provider_transaction_id": attempt.provider_transaction_id if attempt else "",
        "error_code": attempt.error_code if attempt else "",
        "error_message": attempt.error_message if attempt else "",
        "expires_at": _iso(attempt.expires_at) if attempt else None,
        "created_at": _iso(intent.created_at),
        "completed_at": _iso(intent.completed_at),
    }


def _get_collection(request, collection_id):
    return PaymentIntent.objects.filter(pk=collection_id, client=request.api_client,
                                        kind=PaymentKind.COLLECTION).first()


def _collection_not_found():
    return error(404, "collection_not_found", "No such collection for this API key.")


@api_endpoint("POST")
def collections_collection(request):
    """Start a collection. With an OTP provider (C-Pay) the payer receives a
    code: submit it to /collections/<id>/otp/. Same idempotency_key = same
    collection returned (safe to retry after a timeout)."""
    b = request.json
    required = ["source_ref", "payer_ref", "payee_ref", "payer_account", "amount",
                "reference", "idempotency_key", "provider"]
    missing = [k for k in required if not str(b.get(k) or "").strip()]
    if missing:
        return error(400, "invalid_request", f"Missing: {', '.join(missing)}.")
    if b["provider"] not in all_adapters():
        return error(400, "unknown_provider", f"Unknown provider {b['provider']!r}.")
    try:
        amount = Decimal(str(b["amount"]))
    except (InvalidOperation, ValueError):
        return error(400, "invalid_request", "amount must be a number, e.g. \"450.00\".")
    if amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        return error(400, "invalid_request", "amount must be positive, with at most 2 decimals.")

    intent = PaymentService.collect(
        source_ref=b["source_ref"], payer_ref=b["payer_ref"], payee_ref=b["payee_ref"],
        payer_account=str(b["payer_account"]).strip(), amount=amount,
        reference=b["reference"], narration=b.get("narration", ""),
        idempotency_key=_idem(request, b["idempotency_key"]),
        provider=b["provider"], client=request.api_client)
    data = collection_json(intent)
    otp = _simulated_otp(intent) if intent.required_action == "otp" else None
    if otp:
        data["test_otp"] = otp
    return JsonResponse(data, status=201)


@api_endpoint("GET")
def collection_detail(request, collection_id):
    intent = _get_collection(request, collection_id)
    if intent is None:
        return _collection_not_found()
    intent = PaymentService.refresh(intent)       # asks the provider, if it can
    return JsonResponse(collection_json(intent))


@api_endpoint("POST")
def collection_otp(request, collection_id):
    intent = _get_collection(request, collection_id)
    if intent is None:
        return _collection_not_found()
    otp = str(request.json.get("otp") or "").strip()
    if not otp:
        return error(400, "invalid_request", "otp is required.")
    try:
        intent = PaymentService.submit_otp(intent, otp)
    except PaymentError as exc:
        intent.refresh_from_db()
        if intent.is_final:
            data = collection_json(intent)
            return JsonResponse(data, status=200)     # e.g. expired: report the final state
        return error(400, "otp_not_expected", str(exc))
    return JsonResponse(collection_json(intent))
