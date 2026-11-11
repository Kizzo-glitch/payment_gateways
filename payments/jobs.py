"""
Background jobs. No Celery or Redis needed: run them from a scheduler
(Railway cron, or a systemd timer on Hetzner later):

    python manage.py process_payments      # every minute
    python manage.py reconcile_payments    # daily

They are idempotent, so running them twice is harmless.
"""
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import conf
from .models import (AttemptStatus, EventSource, ExceptionKind, IntentStatus, PaymentAttempt,
                     PaymentIntent)
from .providers.base import Capability
from .providers.registry import get_adapter
from .services import PaymentService, open_exception


def expire_otps(now=None):
    now = now or timezone.now()
    count = 0
    for attempt in PaymentAttempt.objects.filter(status=AttemptStatus.AWAITING_OTP,
                                                 expires_at__lt=now):
        PaymentService._expire(attempt)
        count += 1
    return count


def poll_pending(now=None):
    """Ask providers that support status queries about unfinished attempts."""
    now = now or timezone.now()
    count = 0
    due = PaymentAttempt.objects.filter(
        status__in=[AttemptStatus.PROCESSING, AttemptStatus.UNKNOWN],
        next_poll_at__isnull=False, next_poll_at__lte=now).select_related("intent")
    for attempt in due:
        if get_adapter(attempt.provider).supports(Capability.STATUS_QUERY):
            PaymentService.refresh(attempt.intent)
            count += 1
    return count


def flag_stuck(now=None):
    """Attempts processing too long with no way to query become UNKNOWN cases."""
    now = now or timezone.now()
    cutoff = now - timedelta(minutes=conf.get("PAYMENTS_UNKNOWN_AFTER_MINUTES"))
    count = 0
    for attempt in PaymentAttempt.objects.filter(
            status=AttemptStatus.PROCESSING, updated_at__lt=cutoff).select_related("intent"):
        with transaction.atomic():
            open_exception(ExceptionKind.UNKNOWN_OUTCOME, attempt.intent,
                           f"Still processing after {conf.get('PAYMENTS_UNKNOWN_AFTER_MINUTES')} min")
        count += 1
    return count


def reconcile(now=None):
    """Daily checks. Each finding becomes an open ReconciliationException."""
    now = now or timezone.now()
    found = {}

    # 1. Borrower said "I've paid" but the lender hasn't confirmed.
    stale = now - timedelta(days=conf.get("PAYMENTS_CLAIM_STALE_DAYS"))
    for intent in PaymentIntent.objects.filter(status=IntentStatus.AWAITING_ACTION,
                                               required_action="lender_confirmation"):
        claimed = intent.metadata.get("claimed_at")
        if claimed and claimed < stale.isoformat():
            with transaction.atomic():
                open_exception(ExceptionKind.STALE_CLAIM, intent,
                               f"Claimed {claimed}; lender has not confirmed")
            found["stale_claim"] = found.get("stale_claim", 0) + 1

    # 2. Unknown outcomes with no open case yet.
    for intent in PaymentIntent.objects.filter(status=IntentStatus.UNKNOWN).exclude(
            exceptions__is_resolved=False):
        with transaction.atomic():
            open_exception(ExceptionKind.UNKNOWN_OUTCOME, intent, "Outcome still unknown")
        found["unknown_outcome"] = found.get("unknown_outcome", 0) + 1

    # 3. Marked succeeded but nothing authoritative backs it up.
    evidence = Q(attempts__events__source__in=[
        EventSource.SYNC_RESPONSE, EventSource.WEBHOOK, EventSource.POLL,
        EventSource.LENDER_CONFIRMATION])
    for intent in PaymentIntent.objects.filter(status=IntentStatus.SUCCEEDED).exclude(evidence):
        with transaction.atomic():
            open_exception(ExceptionKind.SUCCESS_WITHOUT_EVIDENCE, intent,
                           "Succeeded without a provider response or confirmation on record")
        found["success_without_evidence"] = found.get("success_without_evidence", 0) + 1

    # Provider statement matching plugs in here once a provider offers
    # settlement reports (add a STATEMENTS capability and adapter method).
    return found
