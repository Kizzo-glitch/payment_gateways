from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from payments import demo, jobs, signals
from payments.exceptions import IdempotencyConflict, NoRouteAvailable, PaymentError
from payments.models import (AttemptStatus, ExceptionKind, IntentStatus, PaymentIntent,
                             ProviderEvent, ReconciliationException, SimWallet)
from payments.services import PaymentService

from .helpers import collect, otp_for


class IdempotencyAndOtpTests(TestCase):
    def setUp(self):
        demo.seed()

    def test_same_key_returns_same_intent(self):
        a = collect(1)
        b = collect(1)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(PaymentIntent.objects.count(), 1)

    def test_same_key_different_amount_conflicts(self):
        collect(1)
        with self.assertRaises(IdempotencyConflict):
            collect(1, amount="999.00")

    def test_wrong_otp_fails_and_moves_no_money(self):
        intent = collect(2, amount="50.00")
        intent = PaymentService.submit_otp(intent, "0000" if otp_for(intent) != "0000" else "1111")
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertEqual(SimWallet.objects.get(code=demo.BORROWER_MSISDN).balance, Decimal("2000.00"))

    def test_otp_cannot_be_submitted_twice(self):
        intent = collect(3)
        PaymentService.submit_otp(intent, otp_for(intent))
        with self.assertRaises(PaymentError):
            PaymentService.submit_otp(intent, otp_for(intent))

    def test_expired_otp_is_failed_by_job(self):
        intent = collect(4)
        attempt = intent.current_attempt()
        attempt.expires_at = timezone.now() - timedelta(minutes=1)
        attempt.save()
        self.assertEqual(jobs.expire_otps(), 1)
        intent.refresh_from_db()
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertEqual(intent.current_attempt().status, AttemptStatus.EXPIRED)

    def test_insufficient_funds(self):
        intent = collect(5, amount="999999.00")
        intent = PaymentService.submit_otp(intent, otp_for(intent))
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertEqual(intent.current_attempt().error_code, "INSUFFICIENT_FUNDS")

    def test_every_step_leaves_evidence(self):
        intent = collect(6)
        PaymentService.submit_otp(intent, otp_for(intent))
        sources = list(ProviderEvent.objects.filter(
            attempt__intent=intent).values_list("event_type", flat=True))
        self.assertEqual(sources, ["initiate", "confirm_otp"])

    def test_signals_fire_on_commit(self):
        received = []
        handler = lambda sender, intent, **kw: received.append(intent.status)  # noqa: E731
        signals.payment_succeeded.connect(handler)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                intent = collect(7)
            with self.captureOnCommitCallbacks(execute=True):
                PaymentService.submit_otp(intent, otp_for(intent))
        finally:
            signals.payment_succeeded.disconnect(handler)
        self.assertEqual(received, [IntentStatus.SUCCEEDED])


class RoutingTests(TestCase):
    def setUp(self):
        demo.seed()

    def test_router_picks_shared_channel(self):
        intent = collect(10, provider=None, payer_channels=["simulator"],
                         payee_channels=["simulator", "bank"])
        self.assertEqual(intent.provider, "simulator")

    def test_router_falls_back_to_manual_when_no_shared_channel(self):
        intent = collect(11, provider=None, payer_channels=["mpesa"], payee_channels=["cpay"])
        self.assertEqual(intent.provider, "manual")
        self.assertEqual(intent.required_action, "lender_confirmation")

    def test_router_skips_blocked_option3_and_notes_why(self):
        intent = collect(12, payee_ref="lender:maluti", provider=None,
                         payer_channels=["simulator"], payee_channels=["simulator"])
        self.assertEqual(intent.provider, "manual")
        self.assertTrue(any("Option 3" in n for n in intent.metadata["routing_notes"]))

    def test_explicit_provider_does_not_silently_fall_back(self):
        from payments.exceptions import PlatformCustodyNotPermitted
        with self.assertRaises(PlatformCustodyNotPermitted):
            collect(13, payee_ref="lender:maluti", provider="simulator")

    def test_disbursement_needs_a_disbursing_provider(self):
        with self.settings(PAYMENTS_ADAPTERS={"cpay": "payments.providers.cpay.CpayAdapter"}):
            from payments.providers import registry
            registry.reset()
            try:
                with self.assertRaises(NoRouteAvailable):
                    PaymentService.disburse(
                        source_ref="loan:1", payer_ref="lender:lesedi",
                        payee_ref="borrower:palesa", payee_account=demo.BORROWER_MSISDN,
                        amount=Decimal("10"), reference="D1", idempotency_key="d1")
            finally:
                registry.reset()


class ManualFlowTests(TestCase):
    """Off-platform payments: the claim/confirm model."""

    def setUp(self):
        demo.seed()
        self.intent = collect(20, provider="manual")

    def test_claim_alone_never_settles(self):
        PaymentService.record_borrower_claim(self.intent, actor_ref="borrower:palesa",
                                             reference="EFT-123")
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.AWAITING_ACTION)

    def test_only_payee_can_confirm(self):
        with self.assertRaises(PaymentError):
            PaymentService.confirm_manual(self.intent, actor_ref="borrower:palesa")

    def test_lender_confirmation_settles(self):
        intent = PaymentService.confirm_manual(self.intent, actor_ref="lender:lesedi",
                                               provider_reference="BANK-778")
        self.assertEqual(intent.status, IntentStatus.SUCCEEDED)

    def test_denial_after_claim_opens_dispute(self):
        PaymentService.record_borrower_claim(self.intent, actor_ref="borrower:palesa",
                                             reference="EFT-123")
        intent = PaymentService.confirm_manual(self.intent, actor_ref="lender:lesedi",
                                               received=False)
        self.assertEqual(intent.status, IntentStatus.FAILED)
        self.assertTrue(ReconciliationException.objects.filter(
            kind=ExceptionKind.CLAIM_DISPUTED, intent=intent).exists())

    def test_stale_claim_is_flagged_by_reconcile(self):
        PaymentService.record_borrower_claim(self.intent, actor_ref="borrower:palesa",
                                             reference="EFT-123")
        later = timezone.now() + timedelta(days=5)
        found = jobs.reconcile(now=later)
        self.assertEqual(found.get("stale_claim"), 1)
        jobs.reconcile(now=later)  # idempotent: no second open case
        self.assertEqual(ReconciliationException.objects.filter(
            kind=ExceptionKind.STALE_CLAIM).count(), 1)
