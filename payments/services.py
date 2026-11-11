"""
PaymentService: the only entry point the rest of Fedha-Grow uses.

    intent = PaymentService.collect(
        source_ref="repayment:4821", payer_ref="borrower:77", payee_ref="lender:12",
        payer_account="26658000000", amount=Decimal("450.00"),
        reference="FG-4821-03", idempotency_key="repayment:4821:attempt:1",
        payer_channels=["cpay", "mpesa"], payee_channels=["cpay", "bank"])

    if intent.required_action == "otp":
        PaymentService.submit_otp(intent, otp_from_borrower)

Results are announced through payments.signals; lending code never reads
provider responses directly.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf, router, settlement, signals, state
from .exceptions import IdempotencyConflict, PaymentError, ProviderTimeout
from .models import (AttemptStatus, EventSource, ExceptionKind, IntentStatus,
                     MerchantAccount, PaymentAttempt, PaymentIntent, PaymentKind,
                     ProviderEvent, ReconciliationException)
from .providers.base import (Capability, CollectionRequest, DisbursementRequest, Outcome,
                             ProviderResponse)
from .providers.registry import get_adapter


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _new_ext_id():
    return "FG" + uuid.uuid4().hex[:20].upper()


def open_exception(kind, intent=None, detail="", event=None):
    """Open a reconciliation exception (at most one open per kind per intent)."""
    try:
        with transaction.atomic():
            exc, created = ReconciliationException.objects.get_or_create(
                kind=kind, intent=intent, is_resolved=False,
                defaults={"detail": detail, "event": event})
    except IntegrityError:
        return ReconciliationException.objects.get(kind=kind, intent=intent, is_resolved=False)
    if created:
        transaction.on_commit(lambda: signals.payment_needs_attention.send(
            sender=PaymentService, intent=intent, exception=exc))
    return exc


class PaymentService:

    # ------------------------------------------------------------------
    # Starting payments
    # ------------------------------------------------------------------
    @classmethod
    def collect(cls, *, source_ref, payer_ref, payee_ref, amount, reference, idempotency_key,
                payer_account="", currency=None, narration="", provider=None,
                payer_channels=None, payee_channels=None, metadata=None) -> PaymentIntent:
        """Borrower pays lender. Funds settle to the lender (never Fedha-Grow)."""
        return cls._start(PaymentKind.COLLECTION, Capability.COLLECT, locals())

    @classmethod
    def disburse(cls, *, source_ref, payer_ref, payee_ref, amount, reference, idempotency_key,
                 payee_account="", currency=None, narration="", provider=None,
                 payer_channels=None, payee_channels=None, metadata=None) -> PaymentIntent:
        """Lender pays borrower, from the lender's own account."""
        return cls._start(PaymentKind.DISBURSEMENT, Capability.DISBURSE, locals())

    @classmethod
    def _start(cls, kind, capability, args) -> PaymentIntent:
        amount = Decimal(str(args["amount"]))
        if amount <= 0:
            raise PaymentError("Amount must be positive")
        currency = args.get("currency") or conf.get("PAYMENTS_DEFAULT_CURRENCY")

        existing = PaymentIntent.objects.filter(idempotency_key=args["idempotency_key"]).first()
        if existing:
            same = (existing.kind == kind and existing.source_ref == args["source_ref"]
                    and existing.payee_ref == args["payee_ref"] and existing.amount == amount)
            if not same:
                raise IdempotencyConflict(f"Key {args['idempotency_key']} was used for another payment")
            return existing

        # The lender is the merchant side: payee for collections, payer for disbursements.
        merchant_party = args["payee_ref"] if kind == PaymentKind.COLLECTION else args["payer_ref"]
        route = router.choose(capability, merchant_party, provider=args.get("provider"),
                              payer_channels=args.get("payer_channels"),
                              payee_channels=args.get("payee_channels"))
        ctx = route.merchant
        merchant_obj = None
        if ctx:
            merchant_obj = MerchantAccount.objects.filter(
                provider=route.provider, merchant_code=ctx.settles_to).first()

        metadata = dict(args.get("metadata") or {})
        if route.notes:
            metadata["routing_notes"] = route.notes

        with transaction.atomic():
            intent = PaymentIntent.objects.create(
                kind=kind, source_ref=args["source_ref"], payer_ref=args["payer_ref"],
                payee_ref=args["payee_ref"], payer_account=args.get("payer_account", ""),
                payee_account=args.get("payee_account", ""), amount=amount, currency=currency,
                reference=args["reference"], narration=args.get("narration", ""),
                idempotency_key=args["idempotency_key"], provider=route.provider,
                merchant=merchant_obj,
                settlement_mode=ctx.settlement_mode if ctx else "",
                settles_to=ctx.settles_to if ctx else "", metadata=metadata)
            attempt = PaymentAttempt.objects.create(
                intent=intent, provider=route.provider, ext_transaction_id=_new_ext_id())
        # Committed before calling out: if we crash mid-call, the record exists.

        adapter = get_adapter(route.provider)
        try:
            if kind == PaymentKind.COLLECTION:
                resp = adapter.initiate_collection(cls._collection_request(intent, attempt, ctx))
            else:
                resp = adapter.initiate_disbursement(cls._disbursement_request(intent, attempt, ctx))
        except ProviderTimeout as exc:
            resp = ProviderResponse(Outcome.UNKNOWN, error_code="TIMEOUT", message=str(exc))
        return cls._apply(intent, attempt, resp, EventSource.SYNC_RESPONSE, "initiate")

    # ------------------------------------------------------------------
    # Customer / counterparty actions
    # ------------------------------------------------------------------
    @classmethod
    def submit_otp(cls, intent: PaymentIntent, otp: str) -> PaymentIntent:
        intent.refresh_from_db()
        attempt = intent.current_attempt()
        if intent.required_action != "otp" or attempt.status != AttemptStatus.AWAITING_OTP:
            raise PaymentError("This payment is not waiting for an OTP")
        if attempt.expires_at and attempt.expires_at < timezone.now():
            cls._expire(attempt)
            raise PaymentError("The OTP has expired; start the payment again")
        ctx = cls._context_for(intent)
        adapter = get_adapter(intent.provider)
        try:
            resp = adapter.submit_otp(cls._collection_request(intent, attempt, ctx),
                                      attempt.provider_transaction_id, otp)
        except ProviderTimeout as exc:
            resp = ProviderResponse(Outcome.UNKNOWN, error_code="TIMEOUT", message=str(exc))
        return cls._apply(intent, attempt, resp, EventSource.SYNC_RESPONSE, "confirm_otp")

    @classmethod
    def record_borrower_claim(cls, intent: PaymentIntent, *, actor_ref, reference, note=""):
        """Borrower says "I've paid". Evidence only; it never settles a payment."""
        if intent.is_final:
            raise PaymentError("Payment is already final")
        attempt = intent.current_attempt()
        ProviderEvent.objects.create(
            provider=intent.provider, attempt=attempt, source=EventSource.BORROWER_CLAIM,
            event_type="claim", actor_ref=actor_ref,
            dedupe_key=f"claim:{attempt.ext_transaction_id}:{uuid.uuid4().hex}",
            payload={"reference": reference, "note": note})
        intent.metadata["claimed_at"] = timezone.now().isoformat()
        intent.metadata["claimed_reference"] = reference
        intent.save(update_fields=["metadata", "updated_at"])
        return intent

    @classmethod
    def confirm_manual(cls, intent: PaymentIntent, *, actor_ref, received=True,
                       provider_reference="", note="") -> PaymentIntent:
        """The receiving party confirms (or denies) an off-platform payment.
        Only the payee may confirm: the lender for repayments, the borrower
        for disbursements."""
        intent.refresh_from_db()
        if intent.provider != router.MANUAL:
            raise PaymentError("Only manual payments are confirmed by hand")
        if actor_ref != intent.payee_ref:
            raise PaymentError("Only the receiving party can confirm this payment")
        attempt = intent.current_attempt()
        outcome = Outcome.SUCCEEDED if received else Outcome.FAILED
        resp = ProviderResponse(outcome, provider_transaction_id=provider_reference,
                                error_code="" if received else "NOT_RECEIVED",
                                message=note, raw={"received": received,
                                                   "reference": provider_reference})
        intent = cls._apply(intent, attempt, resp, EventSource.LENDER_CONFIRMATION,
                            "confirm" if received else "deny", actor_ref=actor_ref)
        if not received and intent.metadata.get("claimed_at"):
            with transaction.atomic():
                open_exception(ExceptionKind.CLAIM_DISPUTED, intent,
                               "Borrower claimed payment; receiver says it did not arrive.")
        return intent

    # ------------------------------------------------------------------
    # Finding out what happened
    # ------------------------------------------------------------------
    @classmethod
    def refresh(cls, intent: PaymentIntent) -> PaymentIntent:
        """Ask the provider for the latest status, if it supports that."""
        adapter = get_adapter(intent.provider)
        attempt = intent.current_attempt()
        if not adapter.supports(Capability.STATUS_QUERY) or attempt.status in state.FINAL_ATTEMPT:
            return intent
        ctx = cls._context_for(intent) if intent.settles_to else None
        try:
            resp = adapter.query_status(attempt.ext_transaction_id,
                                        attempt.provider_transaction_id, ctx)
        except ProviderTimeout as exc:
            resp = ProviderResponse(Outcome.UNKNOWN, error_code="TIMEOUT", message=str(exc))
        return cls._apply(intent, attempt, resp, EventSource.POLL, "status")

    @classmethod
    def handle_webhook(cls, provider_code: str, headers: dict, body: bytes):
        """Verify, store, then apply a provider callback. Safe to receive the
        same webhook many times. Returns the intent, or None if unmatched/duplicate."""
        adapter = get_adapter(provider_code)
        result = adapter.parse_webhook(headers, body)  # raises WebhookVerificationFailed
        attempt = (PaymentAttempt.objects.select_related("intent")
                   .filter(provider=provider_code,
                           ext_transaction_id=result.ext_transaction_id).first())
        try:
            with transaction.atomic():
                event = ProviderEvent.objects.create(
                    provider=provider_code, attempt=attempt, source=EventSource.WEBHOOK,
                    event_type=result.event_type, dedupe_key=result.dedupe_key,
                    payload=_jsonable(result.raw))
        except IntegrityError:
            return None  # duplicate delivery; already processed
        if attempt is None:
            with transaction.atomic():
                open_exception(ExceptionKind.UNMATCHED_EVENT, None,
                               f"{provider_code} webhook for unknown {result.ext_transaction_id}",
                               event=event)
            return None
        resp = ProviderResponse(result.outcome, result.provider_transaction_id,
                                amount=result.amount, raw=result.raw)
        return cls._apply(attempt.intent, attempt, resp, EventSource.WEBHOOK,
                          result.event_type, event=event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _collection_request(intent, attempt, ctx):
        return CollectionRequest(attempt.ext_transaction_id, intent.amount, intent.currency,
                                 intent.payer_account, intent.reference, intent.narration, ctx)

    @staticmethod
    def _disbursement_request(intent, attempt, ctx):
        return DisbursementRequest(attempt.ext_transaction_id, intent.amount, intent.currency,
                                   intent.payee_account, intent.reference, intent.narration, ctx)

    @staticmethod
    def _context_for(intent):
        """Re-resolve credentials, and refuse if the settlement destination
        changed since the intent was created."""
        if intent.provider == router.MANUAL:
            return None
        party = intent.payee_ref if intent.kind == PaymentKind.COLLECTION else intent.payer_ref
        ctx = settlement.resolve(intent.provider, party)
        if ctx.settles_to != intent.settles_to:
            raise PaymentError("Settlement destination changed since this payment started")
        return ctx

    @classmethod
    def _expire(cls, attempt):
        with transaction.atomic():
            attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)
            intent = PaymentIntent.objects.select_for_update().get(pk=attempt.intent_id)
            if attempt.status in state.FINAL_ATTEMPT:
                return intent
            state.move_attempt(attempt, AttemptStatus.EXPIRED)
            attempt.error_code = "EXPIRED"
            attempt.save(update_fields=["error_code"])
            state.move_intent(intent, IntentStatus.FAILED)
            transaction.on_commit(lambda: signals.payment_failed.send(
                sender=cls, intent=intent))
        return intent

    @classmethod
    def _apply(cls, intent, attempt, resp: ProviderResponse, source, event_type,
               actor_ref="", event=None) -> PaymentIntent:
        with transaction.atomic():
            if event is None:
                ProviderEvent.objects.create(
                    provider=intent.provider, attempt=attempt, source=source,
                    event_type=event_type, actor_ref=actor_ref,
                    dedupe_key=f"{source}:{attempt.ext_transaction_id}:{uuid.uuid4().hex}",
                    payload=_jsonable({"outcome": resp.outcome.value,
                                       "provider_transaction_id": resp.provider_transaction_id,
                                       "error_code": resp.error_code,
                                       "message": resp.message, "raw": resp.raw}))
            intent = PaymentIntent.objects.select_for_update().get(pk=intent.pk)
            attempt = PaymentAttempt.objects.select_for_update().get(pk=attempt.pk)

            if attempt.status in state.FINAL_ATTEMPT:
                return intent  # late or repeated news about a finished attempt

            if resp.provider_transaction_id and not attempt.provider_transaction_id:
                attempt.provider_transaction_id = resp.provider_transaction_id
            if resp.error_code:
                attempt.error_code = resp.error_code[:64]
                attempt.error_message = (resp.message or "")[:255]
            attempt.save()

            now = timezone.now()
            supports_query = get_adapter(intent.provider).supports(Capability.STATUS_QUERY)
            notify = None

            if resp.outcome == Outcome.SUCCEEDED:
                state.move_attempt(attempt, AttemptStatus.SUCCEEDED)
                if resp.amount is not None and Decimal(resp.amount) != intent.amount:
                    state.move_intent(intent, IntentStatus.UNKNOWN)
                    open_exception(ExceptionKind.AMOUNT_MISMATCH, intent,
                                   f"Provider reported {resp.amount}, expected {intent.amount}")
                else:
                    state.move_intent(intent, IntentStatus.SUCCEEDED)
                    notify = signals.payment_succeeded

            elif resp.outcome == Outcome.FAILED:
                state.move_attempt(attempt, AttemptStatus.FAILED)
                state.move_intent(intent, IntentStatus.FAILED)
                notify = signals.payment_failed

            elif resp.outcome == Outcome.PENDING:
                state.move_attempt(attempt, AttemptStatus.PROCESSING, save=False)
                attempt.next_poll_at = now + timedelta(
                    seconds=conf.get("PAYMENTS_POLL_INTERVAL_SECONDS"))
                attempt.save()
                state.move_intent(intent, IntentStatus.PENDING)

            elif resp.outcome == Outcome.ACTION_REQUIRED:
                if resp.action == "otp":
                    state.move_attempt(attempt, AttemptStatus.AWAITING_OTP, save=False)
                    attempt.expires_at = now + timedelta(
                        minutes=conf.get("PAYMENTS_OTP_EXPIRY_MINUTES"))
                    attempt.save()
                else:
                    state.move_attempt(attempt, AttemptStatus.AWAITING_CONFIRMATION)
                state.move_intent(intent, IntentStatus.AWAITING_ACTION, save=False)
                intent.required_action = resp.action
                intent.save()
                notify = signals.payment_action_required

            else:  # UNKNOWN
                state.move_attempt(attempt, AttemptStatus.UNKNOWN, save=False)
                attempt.next_poll_at = now if supports_query else None
                attempt.save()
                state.move_intent(intent, IntentStatus.UNKNOWN)
                if not supports_query:
                    open_exception(ExceptionKind.UNKNOWN_OUTCOME, intent,
                                   f"{intent.provider} gave no clear answer and has no status "
                                   f"query. Check with the provider. ({resp.message})")

            if notify is not None:
                final_intent = intent
                transaction.on_commit(lambda: notify.send(sender=cls, intent=final_intent))
        return intent
