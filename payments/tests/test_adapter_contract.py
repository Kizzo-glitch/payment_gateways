"""
The adapter contract. Every provider adapter must pass these checks.

When the EcoCash or M-Pesa adapter is written, add a subclass at the bottom
(about ten lines) and this suite tells you whether it fits the rest of the
app without touching any other code.
"""
from decimal import Decimal

from django.test import TestCase

from payments import demo, settlement
from payments.exceptions import CapabilityNotSupported
from payments.providers.base import (Capability, CollectionRequest, DisbursementRequest,
                                     MerchantContext, Outcome, ProviderAdapter, ProviderResponse)
from payments.providers.registry import all_adapters, get_adapter

from .helpers import FakeResponse, FakeSession

SETTLEMENT_CAPS = {Capability.DIRECT_MERCHANT, Capability.SUB_MERCHANTS,
                   Capability.SPLIT_SETTLEMENT}


class AdapterContract:
    adapter_code = ""

    def make_adapter(self):
        return get_adapter(self.adapter_code)

    def collection_request(self) -> CollectionRequest:
        raise NotImplementedError

    # ---------------------------------------------------------------
    def test_is_registered_under_its_code(self):
        self.assertIs(type(get_adapter(self.adapter_code)), type(self.make_adapter()))
        self.assertEqual(self.make_adapter().code, self.adapter_code)

    def test_capabilities_are_known(self):
        for cap in self.make_adapter().capabilities:
            self.assertIsInstance(cap, Capability)

    def test_merchant_providers_declare_a_settlement_model(self):
        a = self.make_adapter()
        if a.supports(Capability.NEEDS_MERCHANT):
            self.assertTrue(a.capabilities & SETTLEMENT_CAPS,
                            "Declare at least one of DIRECT_MERCHANT / SUB_MERCHANTS / SPLIT")

    def test_undeclared_capabilities_raise(self):
        a = self.make_adapter()
        if not a.supports(Capability.DISBURSE):
            with self.assertRaises(CapabilityNotSupported):
                a.initiate_disbursement(DisbursementRequest(
                    "X", Decimal("1"), "LSL", "1", "r", "", None))
        if not a.supports(Capability.STATUS_QUERY):
            with self.assertRaises(CapabilityNotSupported):
                a.query_status("X", "", None)
        if not a.supports(Capability.WEBHOOKS):
            with self.assertRaises(CapabilityNotSupported):
                a.parse_webhook({}, b"{}")

    def test_collection_response_is_well_formed(self):
        a = self.make_adapter()
        if not a.supports(Capability.COLLECT):
            return
        resp = a.initiate_collection(self.collection_request())
        self.assertIsInstance(resp, ProviderResponse)
        self.assertIsInstance(resp.outcome, Outcome)
        if resp.outcome == Outcome.ACTION_REQUIRED:
            self.assertTrue(resp.action, "ACTION_REQUIRED must say which action")
            if resp.action == "otp":
                self.assertIsNot(type(a).submit_otp, ProviderAdapter.submit_otp,
                                 "OTP flow needs submit_otp()")
        if resp.outcome == Outcome.PENDING:
            self.assertTrue(a.supports(Capability.STATUS_QUERY) or a.supports(Capability.WEBHOOKS),
                            "A PENDING payment needs a way to finish: status query or webhooks")


def _sim_request():
    ctx = settlement.resolve("simulator", "lender:lesedi")
    return CollectionRequest("FGCONTRACT1", Decimal("10"), "LSL", demo.BORROWER_MSISDN,
                             "FG-C", "", ctx)


class SimulatorContract(AdapterContract, TestCase):
    adapter_code = "simulator"

    def setUp(self):
        demo.seed()

    def collection_request(self):
        return _sim_request()


class AsyncSimulatorContract(AdapterContract, TestCase):
    adapter_code = "simulator_async"

    def setUp(self):
        demo.seed()

    def collection_request(self):
        ctx = settlement.resolve("simulator_async", "lender:lesedi")
        return CollectionRequest("FGCONTRACT2", Decimal("10"), "LSL", demo.BORROWER_MSISDN,
                                 "FG-C", "", ctx)


class ManualContract(AdapterContract, TestCase):
    adapter_code = "manual"

    def collection_request(self):
        return CollectionRequest("FGCONTRACT3", Decimal("10"), "LSL", "", "FG-C", "", None)


class CpayContract(AdapterContract, TestCase):
    adapter_code = "cpay"

    def make_adapter(self):
        from payments.providers.cpay import CpayAdapter
        return CpayAdapter(session=FakeSession(FakeResponse({"Status": "OTP_SENT"})))

    def collection_request(self):
        ctx = MerchantContext("direct", "8970", {"client_code": "C", "client_secret": "s"},
                              "8970", "lender:x")
        return CollectionRequest("FGCONTRACT4", Decimal("10"), "LSL", "2665", "FG-C", "", ctx)


# class EcocashContract(AdapterContract, TestCase):
#     adapter_code = "ecocash"
#     def collection_request(self): ...


class RegistryTests(TestCase):
    def test_every_registered_adapter_has_a_contract_test(self):
        tested = {c.adapter_code for c in AdapterContract.__subclasses__()}
        missing = set(all_adapters()) - tested
        self.assertFalse(missing, f"Add a contract test for: {missing}")
