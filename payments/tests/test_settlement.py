"""The regulatory core: where money lands under each settlement option."""
from decimal import Decimal

from django.test import TestCase, override_settings

from payments import demo, settlement
from payments.exceptions import (CapabilityNotSupported, MerchantNotConfigured,
                                 PlatformCustodyNotPermitted)
from payments.models import IntentStatus, MerchantAccount, SettlementMode, SimTransaction, SimWallet
from payments.services import PaymentService

from .helpers import collect, otp_for


def bal(code):
    return SimWallet.objects.get(code=code).balance


class SettlementOptionsTests(TestCase):
    def setUp(self):
        demo.seed()

    def test_option1_direct_settles_to_lender_using_lender_credentials(self):
        ctx = settlement.resolve("simulator", "lender:lesedi")
        self.assertEqual(ctx.settlement_mode, SettlementMode.DIRECT)
        self.assertEqual(ctx.initiating_code, "LND-LESEDI")
        self.assertEqual(ctx.settles_to, "LND-LESEDI")
        intent = collect(1, "lender:lesedi", "450.00")
        intent = PaymentService.submit_otp(intent, otp_for(intent))
        self.assertEqual(intent.status, IntentStatus.SUCCEEDED)
        self.assertEqual(bal("LND-LESEDI"), Decimal("5450.00"))
        self.assertEqual(bal(demo.PLATFORM_CODE), Decimal("0.00"))

    def test_option2_sub_merchant_platform_initiates_lender_receives(self):
        ctx = settlement.resolve("simulator", "lender:thaba")
        self.assertEqual(ctx.initiating_code, demo.PLATFORM_CODE)
        self.assertEqual(ctx.settles_to, "LND-THABA")
        intent = collect(2, "lender:thaba", "300.00")
        PaymentService.submit_otp(intent, otp_for(intent))
        self.assertEqual(bal("LND-THABA"), Decimal("5300.00"))
        self.assertEqual(bal(demo.PLATFORM_CODE), Decimal("0.00"))
        txn = SimTransaction.objects.get(
            ext_transaction_id=intent.current_attempt().ext_transaction_id)
        self.assertEqual(txn.settled_via, "")  # never passed through Fedha-Grow

    def test_option3_blocked_by_default(self):
        with self.assertRaises(PlatformCustodyNotPermitted):
            settlement.resolve("simulator", "lender:maluti")
        with self.assertRaises(PlatformCustodyNotPermitted):
            collect(3, "lender:maluti")

    @override_settings(PAYMENTS_ALLOW_PLATFORM_SPLIT=True)
    def test_option3_when_enabled_passes_through_platform(self):
        intent = collect(4, "lender:maluti", "200.00")
        PaymentService.submit_otp(intent, otp_for(intent))
        txn = SimTransaction.objects.get(
            ext_transaction_id=intent.current_attempt().ext_transaction_id)
        self.assertEqual(txn.settled_via, demo.PLATFORM_CODE)
        self.assertEqual(bal("LND-MALUTI"), Decimal("5200.00"))

    def test_guard_refuses_lender_settling_to_platform_code(self):
        """Belt and braces: if config ever pointed a lender's money at a
        Fedha-Grow merchant code, the guard refuses it."""
        from payments.providers.base import MerchantContext
        ctx = MerchantContext(SettlementMode.DIRECT, demo.PLATFORM_CODE, {},
                              demo.PLATFORM_CODE, "lender:lesedi")
        with self.assertRaises(PlatformCustodyNotPermitted):
            settlement._guard_no_custody("simulator", ctx)

    def test_platform_collecting_its_own_fees_is_allowed(self):
        from payments.providers.base import MerchantContext
        ctx = MerchantContext(SettlementMode.DIRECT, demo.PLATFORM_CODE, {}, demo.PLATFORM_CODE,
                              settlement.platform_ref())
        settlement._guard_no_custody("simulator", ctx)  # no exception

    def test_sub_merchant_without_platform_parent_is_rejected(self):
        MerchantAccount.objects.filter(party_ref="lender:thaba", provider="simulator").update(parent=None)
        with self.assertRaises(MerchantNotConfigured):
            settlement.resolve("simulator", "lender:thaba")

    def test_direct_without_credentials_is_rejected(self):
        MerchantAccount.objects.filter(party_ref="lender:lesedi",
                                       provider="simulator").update(credentials_encrypted="")
        with self.assertRaises(MerchantNotConfigured):
            settlement.resolve("simulator", "lender:lesedi")

    def test_provider_without_sub_merchant_support_rejects_option2(self):
        MerchantAccount.objects.create(party_ref=settlement.platform_ref(), provider="cpay",
                                       merchant_code="8970")
        platform = MerchantAccount.objects.get(provider="cpay", merchant_code="8970")
        MerchantAccount.objects.create(party_ref="lender:thaba", provider="cpay",
                                       merchant_code="CP-THABA", parent=platform,
                                       settlement_mode=SettlementMode.SUB_MERCHANT)
        with self.assertRaises(CapabilityNotSupported):
            settlement.resolve("cpay", "lender:thaba")

    def test_simulator_itself_rejects_settling_to_someone_elses_code(self):
        """Even if our side were wrong, the provider-side check holds (as we're asking C-Pay to)."""
        from payments.providers.base import CollectionRequest, MerchantContext, Outcome
        from payments.providers.registry import get_adapter
        ctx = MerchantContext(SettlementMode.DIRECT, "LND-LESEDI",
                              {"client_code": "C-LND-LESEDI", "client_secret": "LND-LESEDI-secret"},
                              "LND-THABA", "lender:thaba")
        resp = get_adapter("simulator").initiate_collection(CollectionRequest(
            "EXT1", Decimal("10"), "LSL", demo.BORROWER_MSISDN, "r", "", ctx))
        self.assertEqual(resp.outcome, Outcome.FAILED)
        self.assertEqual(resp.error_code, "NOT_AUTHORISED")

    def test_simulator_rejects_sub_merchant_settlement_to_non_child(self):
        """Option 2 only works for lenders registered under Fedha-Grow's platform."""
        from payments.providers.base import CollectionRequest, MerchantContext, Outcome
        from payments.providers.registry import get_adapter
        ctx = MerchantContext(SettlementMode.SUB_MERCHANT, demo.PLATFORM_CODE,
                              {"client_code": "FG8970", "client_secret": "fg-secret"},
                              "LND-LESEDI", "lender:lesedi")  # Lesedi is not a sub-merchant
        resp = get_adapter("simulator").initiate_collection(CollectionRequest(
            "EXT2", Decimal("10"), "LSL", demo.BORROWER_MSISDN, "r", "", ctx))
        self.assertEqual(resp.outcome, Outcome.FAILED)
        self.assertEqual(resp.error_code, "NOT_SUB_MERCHANT")
