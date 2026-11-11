"""The handset-approval pattern M-Pesa and EcoCash use, via the async simulator."""
from django.test import TestCase
from django.utils import timezone

from payments import demo, jobs
from payments.models import ExceptionKind, IntentStatus, ReconciliationException
from payments.providers.simulator import AsyncSimulatorAdapter
from payments.services import PaymentService

from .helpers import collect


class AsyncFlowTests(TestCase):
    def setUp(self):
        demo.seed()
        self.intent = collect(1, provider="simulator_async")
        self.ext = self.intent.current_attempt().ext_transaction_id

    def test_starts_pending(self):
        self.assertEqual(self.intent.status, IntentStatus.PENDING)

    def test_poll_resolves_after_customer_approves(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=True)
        jobs.poll_pending(now=timezone.now() + timezone.timedelta(minutes=5))
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.SUCCEEDED)

    def test_poll_resolves_decline(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=False)
        jobs.poll_pending(now=timezone.now() + timezone.timedelta(minutes=5))
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.FAILED)

    def test_webhook_via_http_endpoint_and_dedupe(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=True)
        headers, body = AsyncSimulatorAdapter.build_webhook(self.ext)
        for _ in range(2):
            r = self.client.post("/payments/webhooks/simulator_async/", data=body,
                                 content_type="application/json",
                                 headers={"X-Signature": headers["X-Signature"]})
            self.assertEqual(r.status_code, 200)
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.SUCCEEDED)
        self.assertEqual(self.intent.current_attempt().events.filter(source="webhook").count(), 1)

    def test_bad_signature_rejected(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=True)
        _, body = AsyncSimulatorAdapter.build_webhook(self.ext)
        r = self.client.post("/payments/webhooks/simulator_async/", data=body,
                             content_type="application/json", headers={"X-Signature": "forged"})
        self.assertEqual(r.status_code, 401)
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.PENDING)

    def test_webhook_for_unknown_transaction_opens_exception(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=True)
        headers, body = AsyncSimulatorAdapter.build_webhook(self.ext)
        body = body.replace(self.ext.encode(), b"FGNOTOURS000000000000")
        import hashlib, hmac  # noqa: E401
        headers["X-Signature"] = hmac.new(b"sim-webhook-secret", body, hashlib.sha256).hexdigest()
        self.assertIsNone(PaymentService.handle_webhook("simulator_async", headers, body))
        self.assertTrue(ReconciliationException.objects.filter(
            kind=ExceptionKind.UNMATCHED_EVENT).exists())

    def test_late_webhook_after_final_state_is_ignored(self):
        AsyncSimulatorAdapter.customer_responds(self.ext, approve=True)
        PaymentService.refresh(self.intent)
        headers, body = AsyncSimulatorAdapter.build_webhook(self.ext)
        PaymentService.handle_webhook("simulator_async", headers, body)
        self.intent.refresh_from_db()
        self.assertEqual(self.intent.status, IntentStatus.SUCCEEDED)
