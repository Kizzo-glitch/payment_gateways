"""
Merchant onboarding: register a client's merchant account, verify it, disable it.

Verification is a M1.00 test payment from the merchant owner's own wallet into
the merchant account. The owner receives the provider's OTP and enters it.
Success proves that:
  * the stored credentials work, and
  * money really settles into the account being registered.

C-Pay has no harmless "check credentials" call (every call starts a payment),
which is why verification is a real, tiny payment rather than a lookup.
"""
import uuid
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import settlement, signals
from .exceptions import CapabilityNotSupported, PaymentError
from .models import (IntentStatus, MerchantAccount, MerchantStatus, PaymentIntent,
                     SettlementMode)
from .providers.base import Capability
from .providers.registry import all_adapters, get_adapter
from .services import PaymentService

VERIFICATION_AMOUNT = Decimal("1.00")
REGISTERABLE_MODES = {SettlementMode.DIRECT, SettlementMode.SUB_MERCHANT}


class MerchantError(PaymentError):
    """A request about a merchant that can't be honoured. `code` is for API clients."""

    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
def register(client, *, party_ref, provider, mode, merchant_code, display_name="",
             credentials=None) -> tuple[MerchantAccount, bool]:
    """
    Create or replace a client's merchant account. Re-registering the same
    party at the same provider replaces the details and resets verification.
    Returns (merchant, created).
    """
    party_ref = (party_ref or "").strip()
    merchant_code = (merchant_code or "").strip()
    if not party_ref or not merchant_code:
        raise MerchantError("party_ref and merchant_code are required.")
    if provider not in all_adapters():
        raise MerchantError(f"Unknown provider {provider!r}.", code="unknown_provider")
    adapter = get_adapter(provider)
    if not adapter.supports(Capability.NEEDS_MERCHANT):
        raise MerchantError(f"{provider} does not use merchant accounts.", code="unknown_provider")
    if mode not in REGISTERABLE_MODES:
        raise MerchantError("mode must be 'direct' or 'sub_merchant'.")

    is_platform = party_ref == settlement.platform_ref()
    parent = None
    credentials = credentials or {}

    if mode == SettlementMode.DIRECT:
        missing = [k for k in ("client_code", "client_secret") if not credentials.get(k)]
        if missing:
            raise MerchantError(f"Direct mode needs credentials: {', '.join(missing)}.")
    else:
        if is_platform:
            raise MerchantError("The platform account itself must use direct mode.")
        parent = settlement.platform_account(provider, client)
        if parent is None:
            raise MerchantError(
                f"Register your own '{settlement.platform_ref()}' account at {provider} "
                "before adding sub-merchants.", code="platform_account_missing")
        credentials = {}   # sub-merchants act through the platform's credentials

    clash = (MerchantAccount.objects
             .filter(client=client, provider=provider, merchant_code=merchant_code)
             .exclude(party_ref=party_ref).exists())
    if clash:
        raise MerchantError("That merchant code is already registered to another party.",
                            code="merchant_code_in_use")
    if not is_platform and merchant_code in settlement.platform_merchant_codes(provider):
        raise MerchantError("That merchant code belongs to a platform account.",
                            code="merchant_code_in_use")

    with transaction.atomic():
        merchant = (MerchantAccount.objects.select_for_update()
                    .filter(client=client, party_ref=party_ref, provider=provider).first())
        created = merchant is None
        if created:
            merchant = MerchantAccount(client=client, party_ref=party_ref, provider=provider)
        merchant.merchant_code = merchant_code
        merchant.display_name = display_name or merchant.display_name
        merchant.settlement_mode = mode
        merchant.parent = parent
        merchant.set_credentials(credentials)
        merchant.is_active = True
        merchant.status = MerchantStatus.PENDING
        merchant.verified_at = None
        merchant.last_error = ""
        merchant.verification_intent = None
        merchant.save()
    return merchant, created


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
def start_verification(merchant: MerchantAccount, msisdn: str) -> PaymentIntent:
    """Start the M1.00 test payment from `msisdn` (the owner's own wallet)."""
    msisdn = (msisdn or "").strip()
    if not msisdn:
        raise MerchantError("msisdn (the merchant owner's own wallet number) is required.")
    if merchant.status == MerchantStatus.DISABLED or not merchant.is_active:
        raise MerchantError("This merchant is disabled. Register it again to re-enable.",
                            code="merchant_disabled")
    if merchant.status == MerchantStatus.VERIFIED:
        raise MerchantError("Already verified.", code="already_verified")

    adapter = get_adapter(merchant.provider)
    if (merchant.settlement_mode == SettlementMode.SUB_MERCHANT
            and not adapter.supports(Capability.SUB_MERCHANTS)):
        _mark(merchant, MerchantStatus.FAILED,
              f"{adapter.display_name or merchant.provider} does not support sub-merchants yet.")
        raise MerchantError(merchant.last_error, code="not_supported_by_provider")

    try:
        intent = PaymentService.collect(
            source_ref=f"verification:{merchant.pk}",
            payer_ref=merchant.party_ref, payee_ref=merchant.party_ref,
            payer_account=msisdn, amount=VERIFICATION_AMOUNT,
            reference=f"VERIFY-{merchant.pk}",
            narration="Merchant verification",
            idempotency_key=f"verify:{merchant.pk}:{uuid.uuid4().hex}",
            provider=merchant.provider, client=merchant.client,
            require_verified=False,
            metadata={"verification": True, "merchant_id": merchant.pk})
    except CapabilityNotSupported as exc:
        _mark(merchant, MerchantStatus.FAILED, str(exc))
        raise MerchantError(str(exc), code="not_supported_by_provider") from exc

    MerchantAccount.objects.filter(pk=merchant.pk).update(verification_intent=intent)
    merchant.verification_intent = intent
    sync_from_intent(intent)
    merchant.refresh_from_db()
    return intent


def confirm_verification(merchant: MerchantAccount, otp: str) -> MerchantAccount:
    intent = merchant.verification_intent
    if intent is None or intent.required_action != "otp":
        raise MerchantError("No verification is waiting for an OTP. Start verification first.",
                            code="no_pending_verification")
    otp = (otp or "").strip()
    if not otp:
        raise MerchantError("otp is required.")
    try:
        intent = PaymentService.submit_otp(intent, otp)
    except PaymentError as exc:   # e.g. OTP expired
        sync_from_intent(PaymentIntent.objects.get(pk=intent.pk))
        merchant.refresh_from_db()
        raise MerchantError(str(exc), code="verification_failed") from exc
    sync_from_intent(intent)
    merchant.refresh_from_db()
    return merchant


def sync_from_intent(intent: PaymentIntent):
    """Update the merchant from its verification payment's outcome. Safe to
    call repeatedly; also wired to the payment signals for async providers."""
    if not (intent.metadata or {}).get("verification"):
        return
    merchant = MerchantAccount.objects.filter(pk=intent.metadata.get("merchant_id")).first()
    if merchant is None or merchant.verification_intent_id != intent.pk:
        return   # a newer registration or verification has replaced this one
    if intent.status == IntentStatus.SUCCEEDED:
        _mark(merchant, MerchantStatus.VERIFIED, "")
    elif intent.status in (IntentStatus.FAILED, IntentStatus.CANCELLED):
        attempt = intent.current_attempt()
        reason = (attempt.error_message or attempt.error_code) if attempt else ""
        _mark(merchant, MerchantStatus.FAILED, reason or "Verification payment failed.")


def _mark(merchant, status, error):
    if merchant.status == MerchantStatus.DISABLED:
        return
    merchant.status = status
    merchant.last_error = (error or "")[:255]
    merchant.verified_at = timezone.now() if status == MerchantStatus.VERIFIED else None
    merchant.save(update_fields=["status", "last_error", "verified_at", "updated_at"])


# ---------------------------------------------------------------------------
# Disable
# ---------------------------------------------------------------------------
def disable(merchant: MerchantAccount) -> MerchantAccount:
    """Stop using this merchant. Credentials are wiped; register again to re-enable."""
    merchant.is_active = False
    merchant.status = MerchantStatus.DISABLED
    merchant.set_credentials({})
    merchant.save(update_fields=["is_active", "status", "credentials_encrypted", "updated_at"])
    return merchant


# Async providers (handset approval) finish later, by webhook or status poll.
def _on_payment_finished(sender, intent, **kwargs):
    sync_from_intent(intent)


signals.payment_succeeded.connect(_on_payment_finished, dispatch_uid="merchant_verification_ok")
signals.payment_failed.connect(_on_payment_finished, dispatch_uid="merchant_verification_failed")
