import hashlib
import hmac
from decimal import Decimal

import requests
from django.test import TestCase, override_settings

from payments import settlement
from payments.models import (ExceptionKind, IntentStatus, MerchantAccount,
                             ReconciliationException, SettlementMode)
from payments.providers import registry
from payments.providers.base import Capability, CollectionRequest, MerchantContext, Outcome
from payments.providers.cpay import CpayAdapter, checksum
from payments.services import PaymentService

from .helpers import FakeResponse, FakeSession

CREDS = {"client_code": "FEDHA8970", "client_secret": "s3cret"}


def req(mode=SettlementMode.DIRECT, settles_to="8970"):
    ctx = MerchantContext(mode, "8970", CREDS, settles_to, "lender:x")
    return CollectionRequest("FGEXT1", Decimal("150"), "LSL", "26657000000", "FG-1", "", ctx)


class CpayAdapterTests(TestCase):
    def test_checksum_matches_documented_salt(self):
        expected = hmac.new(b"s3cret", b"FGEXT1FEDHA8970150.0026657000000",
                            hashlib.sha256).hexdigest()
        self.assertEqual(checksum("s3cret", "FGEXT1", "FEDHA8970", "150.00", "26657000000"),
                         expected)

    def test_initiate_body_and_otp_outcome(self):
        session = FakeSession(FakeResponse({"Status": "OTP_SENT", "TransactionId": "CP1"}))
        resp = CpayAdapter(session=session).initiate_collection(req())
        self.assertEqual(resp.outcome, Outcome.ACTION_REQUIRED)
        self.assertEqual(resp.action, "otp")
        body = session.calls[0]["json"]
        self.assertTrue(session.calls[0]["url"].endswith("/api/cpaypayments/payment"))
        self.assertEqual(body["Amount"], "150.00")
        self.assertEqual(body["Checksum"], checksum("s3cret", "FGEXT1", "FEDHA8970",
                                                    "150.00", "26657000000"))
        self.assertNotIn("SubMerchantCode", body)

    def test_confirm_includes_otp_in_checksum(self):
        session = FakeSession(FakeResponse({"Status": "SUCCESS", "TransactionId": "CP1"}))
        resp = CpayAdapter(session=session).submit_otp(req(), "CP1", "4321")
        self.assertEqual(resp.outcome, Outcome.SUCCEEDED)
        body = session.calls[0]["json"]
        self.assertEqual(body["OTP"], "4321")
        self.assertEqual(body["Checksum"], checksum("s3cret", "FGEXT1", "FEDHA8970",
                                                    "150.00", "26657000000", "4321"))

    def test_unrecognised_status_is_unknown_not_failed(self):
        session = FakeSession(FakeResponse({"Status": "WEIRD"}))
        resp = CpayAdapter(session=session).submit_otp(req(), "CP1", "4321")
        self.assertEqual(resp.outcome, Outcome.UNKNOWN)

    def test_http_error_is_failure(self):
        session = FakeSession(FakeResponse({"Message": "bad checksum"}, status=400))
        resp = CpayAdapter(session=session).initiate_collection(req())
        self.assertEqual(resp.outcome, Outcome.FAILED)

    def test_default_capabilities_match_the_docs(self):
        caps = CpayAdapter().capabilities
        self.assertIn(Capability.COLLECT, caps)
        for missing in (Capability.DISBURSE, Capability.STATUS_QUERY, Capability.WEBHOOKS,
                        Capability.SUB_MERCHANTS, Capability.SPLIT_SETTLEMENT):
            self.assertNotIn(missing, caps)

    @override_settings(CPAY={"SUPPORTS_SUB_MERCHANTS": True})
    def test_sub_merchant_mode_sends_destination_when_enabled(self):
        adapter = CpayAdapter(session=FakeSession(FakeResponse({"Status": "OTP_SENT"})))
        self.assertIn(Capability.SUB_MERCHANTS, adapter.capabilities)
        adapter.initiate_collection(req(SettlementMode.SUB_MERCHANT, "CP-THABA"))
        self.assertEqual(adapter.session.calls[0]["json"]["SubMerchantCode"], "CP-THABA")


class CpayThroughServiceTests(TestCase):
    """C-Pay has no status query, so a timeout must become a human review case."""

    def setUp(self):
        m = MerchantAccount(party_ref="lender:lesedi", provider="cpay", merchant_code="CP-LESEDI",
                            settlement_mode=SettlementMode.DIRECT)
        m.set_credentials(CREDS)
        m.save()
        self.adapter = registry.get_adapter("cpay")
        self.original = self.adapter.session

    def tearDown(self):
        self.adapter.session = self.original

    def _start(self, *responses):
        self.adapter.session = FakeSession(*responses)
        return PaymentService.collect(
            source_ref="repayment:1", payer_ref="borrower:1", payee_ref="lender:lesedi",
            payer_account="26657000000", amount=Decimal("150"), reference="FG-1",
            idempotency_key="cpay:1", provider="cpay")

    def test_happy_path(self):
        intent = self._start(FakeResponse({"Status": "OTP_SENT", "TransactionId": "CP1"}),
                             FakeResponse({"Status": "SUCCESS", "TransactionId": "CP1"}))
        self.assertEqual(intent.required_action, "otp")
        self.assertEqual(intent.settles_to, "CP-LESEDI")
        intent = PaymentService.submit_otp(intent, "1234")
        self.assertEqual(intent.status, IntentStatus.SUCCEEDED)

    def test_timeout_on_confirm_becomes_unknown_with_review_case(self):
        intent = self._start(FakeResponse({"Status": "OTP_SENT", "TransactionId": "CP1"}),
                             requests.Timeout("read timed out"))
        intent = PaymentService.submit_otp(intent, "1234")
        self.assertEqual(intent.status, IntentStatus.UNKNOWN)
        self.assertTrue(ReconciliationException.objects.filter(
            intent=intent, kind=ExceptionKind.UNKNOWN_OUTCOME).exists())

    def test_cpay_option2_refused_until_chaperone_confirms(self):
        platform = MerchantAccount.objects.create(party_ref=settlement.platform_ref(),
                                                  provider="cpay", merchant_code="8970")
        MerchantAccount.objects.create(party_ref="lender:thaba", provider="cpay",
                                       merchant_code="CP-THABA", parent=platform,
                                       settlement_mode=SettlementMode.SUB_MERCHANT)
        from payments.exceptions import CapabilityNotSupported
        with self.assertRaises(CapabilityNotSupported):
            settlement.resolve("cpay", "lender:thaba")
