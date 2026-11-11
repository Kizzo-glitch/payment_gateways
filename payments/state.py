"""Allowed status transitions. Every status change goes through here."""
from django.utils import timezone

from .exceptions import InvalidTransition
from .models import AttemptStatus as A
from .models import IntentStatus as I

INTENT_TRANSITIONS = {
    I.CREATED: {I.PENDING, I.AWAITING_ACTION, I.SUCCEEDED, I.FAILED, I.CANCELLED, I.UNKNOWN},
    I.PENDING: {I.AWAITING_ACTION, I.SUCCEEDED, I.FAILED, I.UNKNOWN},
    I.AWAITING_ACTION: {I.PENDING, I.SUCCEEDED, I.FAILED, I.CANCELLED, I.UNKNOWN},
    I.UNKNOWN: {I.PENDING, I.SUCCEEDED, I.FAILED},
    I.SUCCEEDED: set(),
    I.FAILED: set(),
    I.CANCELLED: set(),
}

ATTEMPT_TRANSITIONS = {
    A.INITIATED: {A.AWAITING_OTP, A.AWAITING_CONFIRMATION, A.PROCESSING,
                  A.SUCCEEDED, A.FAILED, A.UNKNOWN},
    A.AWAITING_OTP: {A.PROCESSING, A.SUCCEEDED, A.FAILED, A.EXPIRED, A.UNKNOWN},
    A.AWAITING_CONFIRMATION: {A.SUCCEEDED, A.FAILED, A.EXPIRED},
    A.PROCESSING: {A.SUCCEEDED, A.FAILED, A.UNKNOWN},
    A.UNKNOWN: {A.PROCESSING, A.SUCCEEDED, A.FAILED},
    A.SUCCEEDED: set(),
    A.FAILED: set(),
    A.EXPIRED: set(),
}

FINAL_ATTEMPT = {A.SUCCEEDED, A.FAILED, A.EXPIRED}
FINAL_INTENT = {I.SUCCEEDED, I.FAILED, I.CANCELLED}


def move_intent(intent, new_status, save=True):
    if new_status == intent.status:
        return intent
    if new_status not in INTENT_TRANSITIONS[intent.status]:
        raise InvalidTransition(f"Intent {intent.pk}: {intent.status} -> {new_status} not allowed")
    intent.status = new_status
    if new_status in FINAL_INTENT:
        intent.completed_at = timezone.now()
        intent.required_action = ""
    if save:
        intent.save()
    return intent


def move_attempt(attempt, new_status, save=True):
    if new_status == attempt.status:
        return attempt
    if new_status not in ATTEMPT_TRANSITIONS[attempt.status]:
        raise InvalidTransition(
            f"Attempt {attempt.ext_transaction_id}: {attempt.status} -> {new_status} not allowed")
    attempt.status = new_status
    if new_status in FINAL_ATTEMPT:
        attempt.completed_at = timezone.now()
        attempt.next_poll_at = None
    if save:
        attempt.save()
    return attempt
