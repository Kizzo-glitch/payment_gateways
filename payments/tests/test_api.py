"""Client API v1: authentication, merchant registration, verification, disabling."""
import json
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from payments.exceptions import MerchantNotConfigured, MerchantNotVerified
from payments.models import ApiClient, MerchantAccount, MerchantStatus, SimWallet
from payments.providers.simulator import AsyncSimulatorAdapter
from payments.services import PaymentService

OWNER_PHONE = "26657000001"      # the lender's own C-Pay number
BORROWER_PHONE = "26658000002"


def wallets():
    SimWallet.objects.create(code=OWNER_PHONE, kind=SimWallet.KIND_CUSTOMER,
                             label="Lender owner", balance=Decimal("50.00"))
    SimWallet.objects.create(code=BORROWER_PHONE, kind=SimWallet.KIND_CUSTOMER,
                             label="Borrower", balance=Decimal("500.00"))
    SimWallet.objects.create(code="LND-A", kind=SimWallet.KIND_MERCHANT, label="Lender A",
                             client_code="CA", client_secret="sa")
    SimWallet.objects.create(code="FG-PLAT", kind=SimWallet.KIND_MERCHANT, label="Platform",
                             client_code="CP", client_secret="sp")
    SimWallet.objects.create(code="LND-SUB", kind=SimWallet.KIND_MERCHANT, label="Sub lender",
                             parent_code="FG-PLAT")


def bal(code):
    return SimWallet.objects.get(code=code).balance


class ApiTestCase(TestCase):
    def setUp(self):
        wallets()
        self.client_app, self.key = ApiClient.create_with_key("Fedha-Grow (test)")
        self.other_app, self.other_key = ApiClient.create_with_key("Other app")

    def call(self, method, path, body=None, key="__default__"):
        key = self.key if key == "__default__" else key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if method == "GET":
            r = self.client.get(f"/api/v1/{path}", headers=headers)
        else:
            r = self.client.post(f"/api/v1/{path}", data=json.dumps(body or {}),
                                 content_type="application/json", headers=headers)
        return r.status_code, r.json()

    def register(self, **overrides):
        body = {"party_ref": "lender:1", "provider": "simulator", "mode": "direct",
                "merchant_code": "LND-A", "display_name": "Lender A",
                "credentials": {"client_code": "CA", "client_secret": "sa"}}
        body.update(overrides)
        return self.call("POST", "merchants/", body)

    def verify(self, merchant_id, msisdn=OWNER_PHONE):
        return self.call("POST", f"merchants/{merchant_id}/verify/", {"msisdn": msisdn})


class AuthTests(ApiTestCase):
    def test_health_needs_no_key(self):
        self.assertEqual(self.call("GET", "health/", key=None), (200, {"status": "ok"}))

    def test_missing_and_wrong_keys_rejected(self):
        for key in (None, "pgk_wrong", "not-a-key"):
            status, body = self.call("GET", "merchants/1/", key=key)
            self.assertEqual(status, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")

    def test_deactivated_client_rejected(self):
        self.client_app.is_active = False
        self.client_app.save()
        self.assertEqual(self.register()[0], 401)

    def test_only_a_hash_is_stored(self):
        self.assertNotIn(self.key, self.client_app.key_hash)
        self.assertEqual(self.client_app.key_hash, ApiClient.hash_key(self.key))

    def test_wrong_method_and_bad_json(self):
        self.assertEqual(self.call("GET", "merchants/")[0], 405)
        r = self.client.post("/api/v1/merchants/", data="{not json",
                             content_type="application/json",
                             headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid_json")


class RegisterTests(ApiTestCase):
    def test_register_returns_pending_and_never_credentials(self):
        status, body = self.register()
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], "pending_verification")
        self.assertNotIn("sa", json.dumps(body))
        self.assertNotIn("credentials", body)
        m = MerchantAccount.objects.get(pk=body["id"])
        self.assertEqual(m.client, self.client_app)
        self.assertEqual(m.credentials["client_secret"], "sa")
        self.assertNotIn("sa", m.credentials_encrypted)

    def test_reregistering_replaces_and_resets(self):
        _, first = self.register()
        MerchantAccount.objects.filter(pk=first["id"]).update(status=MerchantStatus.VERIFIED)
        status, second = self.register(display_name="Renamed")
        self.assertEqual(status, 200)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["status"], "pending_verification")

    def test_validation_errors(self):
        cases = [
            ({"credentials": {}}, "invalid_request"),
            ({"provider": "nope"}, "unknown_provider"),
            ({"provider": "manual"}, "unknown_provider"),
            ({"mode": "platform_split"}, "invalid_request"),
            ({"mode": "sub_merchant"}, "platform_account_missing"),
            ({"merchant_code": ""}, "invalid_request"),
        ]
        for overrides, code in cases:
            status, body = self.register(**overrides)
            self.assertEqual((status, body["error"]["code"]), (400, code), overrides)

    def test_merchant_code_cannot_belong_to_two_parties(self):
        self.register()
        status, body = self.register(party_ref="lender:2")
        self.assertEqual((status, body["error"]["code"]), (400, "merchant_code_in_use"))

    def test_lender_cannot_register_a_platform_code(self):
        self.register(party_ref="platform", merchant_code="FG-PLAT",
                      credentials={"client_code": "CP", "client_secret": "sp"})
        status, body = self.register(party_ref="lender:2", merchant_code="FG-PLAT")
        self.assertEqual((status, body["error"]["code"]), (400, "merchant_code_in_use"))

    def test_another_clients_lender_cannot_use_a_platform_code(self):
        """Lender money must never settle into any client's own platform account."""
        self.register(party_ref="platform", merchant_code="FG-PLAT",
                      credentials={"client_code": "CP", "client_secret": "sp"})
        status, body = self.call("POST", "merchants/", {
            "party_ref": "lender:7", "provider": "simulator", "mode": "direct",
            "merchant_code": "FG-PLAT", "credentials": {"client_code": "CP", "client_secret": "sp"},
        }, key=self.other_key)
        self.assertEqual((status, body["error"]["code"]), (400, "merchant_code_in_use"))

    def test_clients_are_isolated(self):
        _, mine = self.register()
        status, body = self.call("GET", f"merchants/{mine['id']}/", key=self.other_key)
        self.assertEqual((status, body["error"]["code"]), (404, "merchant_not_found"))
        # Another client may use the same party_ref for its own lender.
        status, theirs = self.call("POST", "merchants/", {
            "party_ref": "lender:1", "provider": "simulator", "mode": "direct",
            "merchant_code": "LND-A", "credentials": {"client_code": "CA", "client_secret": "sa"},
        }, key=self.other_key)
        self.assertEqual(status, 201)
        self.assertNotEqual(theirs["id"], mine["id"])


class VerificationTests(ApiTestCase):
    def test_m1_test_payment_verifies_merchant(self):
        _, m = self.register()
        status, body = self.verify(m["id"])
        self.assertEqual(status, 202)
        self.assertEqual(body["verification"]["required_action"], "otp")
        self.assertEqual(body["verification"]["amount"], "1.00")
        status, body = self.call("POST", f"merchants/{m['id']}/verify/confirm/",
                                 {"otp": body["test_otp"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "verified")
        self.assertIsNotNone(body["verified_at"])
        self.assertEqual(bal(OWNER_PHONE), Decimal("49.00"))   # M1 moved owner -> own merchant
        self.assertEqual(bal("LND-A"), Decimal("1.00"))

    def test_wrong_credentials_fail_verification(self):
        _, m = self.register(credentials={"client_code": "CA", "client_secret": "WRONG"})
        status, body = self.verify(m["id"])
        self.assertEqual(body["status"], "failed")
        self.assertIn("rejected", body["last_error"])

    def test_wrong_otp_fails_and_can_be_retried(self):
        _, m = self.register()
        _, body = self.verify(m["id"])
        bad = "0000" if body["test_otp"] != "0000" else "1111"
        _, body = self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": bad})
        self.assertEqual(body["status"], "failed")
        _, body = self.verify(m["id"])                                      # retry
        _, body = self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": body["test_otp"]})
        self.assertEqual(body["status"], "verified")

    def test_confirm_without_start(self):
        _, m = self.register()
        status, body = self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": "1234"})
        self.assertEqual((status, body["error"]["code"]), (400, "no_pending_verification"))

    def test_msisdn_required(self):
        _, m = self.register()
        status, body = self.verify(m["id"], msisdn="")
        self.assertEqual(status, 400)

    def test_sub_merchant_verifies_through_platform_credentials(self):
        self.register(party_ref="platform", merchant_code="FG-PLAT",
                      credentials={"client_code": "CP", "client_secret": "sp"})
        _, m = self.register(party_ref="lender:9", mode="sub_merchant", merchant_code="LND-SUB",
                             credentials={})
        _, body = self.verify(m["id"])
        _, body = self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": body["test_otp"]})
        self.assertEqual(body["status"], "verified")
        self.assertEqual(bal("LND-SUB"), Decimal("1.00"))
        self.assertEqual(bal("FG-PLAT"), Decimal("0.00"))           # never touched the platform

    def test_cpay_sub_merchant_reports_not_supported(self):
        self.call("POST", "merchants/", {
            "party_ref": "platform", "provider": "cpay", "mode": "direct", "merchant_code": "8970",
            "credentials": {"client_code": "FEDHA8970", "client_secret": "s"}})
        _, m = self.call("POST", "merchants/", {
            "party_ref": "lender:3", "provider": "cpay", "mode": "sub_merchant",
            "merchant_code": "CP-L3"})
        status, body = self.verify(m["id"])
        self.assertEqual((status, body["error"]["code"]), (400, "not_supported_by_provider"))
        self.assertEqual(MerchantAccount.objects.get(pk=m["id"]).status, MerchantStatus.FAILED)

    def test_async_provider_verifies_when_webhook_arrives(self):
        _, m = self.call("POST", "merchants/", {
            "party_ref": "lender:1", "provider": "simulator_async", "mode": "direct",
            "merchant_code": "LND-A", "credentials": {"client_code": "CA", "client_secret": "sa"}})
        _, body = self.verify(m["id"])
        self.assertEqual(body["verification"]["status"], "pending")
        merchant = MerchantAccount.objects.get(pk=m["id"])
        ext = merchant.verification_intent.current_attempt().ext_transaction_id
        AsyncSimulatorAdapter.customer_responds(ext, approve=True)
        headers, raw = AsyncSimulatorAdapter.build_webhook(ext)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/payments/webhooks/simulator_async/", data=raw,
                             content_type="application/json",
                             headers={"X-Signature": headers["X-Signature"]})
        self.assertEqual(self.call("GET", f"merchants/{m['id']}/")[1]["status"], "verified")


class CollectionGateTests(ApiTestCase):
    def collect(self, n):
        return PaymentService.collect(
            source_ref=f"repayment:{n}", payer_ref="borrower:1", payee_ref="lender:1",
            payer_account=BORROWER_PHONE, amount=Decimal("100"), reference=f"R{n}",
            idempotency_key=f"{self.client_app.pk}:r{n}", provider="simulator",
            client=self.client_app)

    def test_unverified_merchant_cannot_collect_until_verified(self):
        _, m = self.register()
        with self.assertRaises(MerchantNotVerified):
            self.collect(1)
        _, body = self.verify(m["id"])
        self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": body["test_otp"]})
        self.assertEqual(self.collect(2).required_action, "otp")

    def test_disable_stops_collection_and_wipes_credentials(self):
        _, m = self.register()
        _, body = self.verify(m["id"])
        self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": body["test_otp"]})
        status, body = self.call("POST", f"merchants/{m['id']}/disable/")
        self.assertEqual((status, body["status"]), (200, "disabled"))
        self.assertEqual(MerchantAccount.objects.get(pk=m["id"]).credentials, {})
        with self.assertRaises(MerchantNotConfigured):
            self.collect(3)
        self.assertEqual(self.verify(m["id"])[1]["error"]["code"], "merchant_disabled")


class CreateClientCommandTests(TestCase):
    def test_create_and_rotate(self):
        out = StringIO()
        call_command("create_api_client", "Fedha-Grow (local)", stdout=out)
        key = out.getvalue().splitlines()[1].strip()
        client = ApiClient.authenticate(key)
        self.assertEqual(client.name, "Fedha-Grow (local)")
        out = StringIO()
        call_command("create_api_client", "Fedha-Grow (local)", "--rotate", stdout=out)
        new_key = out.getvalue().splitlines()[1].strip()
        self.assertIsNone(ApiClient.authenticate(key))
        self.assertEqual(ApiClient.authenticate(new_key), client)


class CollectionApiTests(ApiTestCase):
    """A borrower pays a verified lender through /api/v1/collections/."""

    def setUp(self):
        super().setUp()
        _, m = self.register()
        _, body = self.verify(m["id"])
        self.call("POST", f"merchants/{m['id']}/verify/confirm/", {"otp": body["test_otp"]})
        self.merchant_id = m["id"]

    def start(self, key="loan7-1", amount="120.00", **overrides):
        body = {"source_ref": "loan:7", "payer_ref": "borrower:3", "payee_ref": "lender:1",
                "payer_account": BORROWER_PHONE, "amount": amount, "reference": "FGP-TEST",
                "idempotency_key": key, "provider": "simulator"}
        body.update(overrides)
        return self.call("POST", "collections/", body)

    def test_full_payment_with_otp(self):
        status, c = self.start()
        self.assertEqual(status, 201)
        self.assertEqual((c["status"], c["required_action"]), ("awaiting_action", "otp"))
        self.assertEqual(c["settles_to"], "LND-A")
        self.assertEqual(c["idempotency_key"], "loan7-1")
        status, c = self.call("POST", f"collections/{c['id']}/otp/", {"otp": c["test_otp"]})
        self.assertEqual((status, c["status"]), (200, "succeeded"))
        self.assertTrue(c["provider_transaction_id"])
        self.assertEqual(bal(BORROWER_PHONE), Decimal("380.00"))
        self.assertEqual(bal("LND-A"), Decimal("121.00"))      # +1 from verification
        self.assertEqual(self.call("GET", f"collections/{c['id']}/")[1]["status"], "succeeded")

    def test_retry_with_same_key_returns_same_collection(self):
        _, first = self.start()
        status, again = self.start()
        self.assertEqual(again["id"], first["id"])
        status, body = self.start(amount="999.00")
        self.assertEqual((status, body["error"]["code"]), (409, "idempotency_conflict"))

    def test_keys_are_per_client(self):
        """Another app using the same key gets its own collection (or its own error)."""
        self.start()
        status, body = self.call("POST", "collections/", {
            "source_ref": "x", "payer_ref": "b", "payee_ref": "lender:1",
            "payer_account": BORROWER_PHONE, "amount": "1.00", "reference": "R",
            "idempotency_key": "loan7-1", "provider": "simulator"}, key=self.other_key)
        self.assertEqual((status, body["error"]["code"]), (400, "merchant_not_found"))

    def test_wrong_otp_fails(self):
        _, c = self.start()
        bad = "0000" if c["test_otp"] != "0000" else "1111"
        status, c = self.call("POST", f"collections/{c['id']}/otp/", {"otp": bad})
        self.assertEqual((status, c["status"]), (200, "failed"))
        self.assertEqual(c["error_code"], "INVALID_OTP")
        self.assertEqual(bal(BORROWER_PHONE), Decimal("500.00"))

    def test_expired_otp_reports_failure(self):
        from datetime import timedelta
        from django.utils import timezone
        from payments.models import PaymentIntent
        _, c = self.start()
        attempt = PaymentIntent.objects.get(pk=c["id"]).current_attempt()
        attempt.expires_at = timezone.now() - timedelta(minutes=1)
        attempt.save()
        status, body = self.call("POST", f"collections/{c['id']}/otp/", {"otp": c["test_otp"]})
        self.assertEqual((status, body["status"]), (200, "failed"))

    def test_otp_after_completion_is_refused(self):
        _, c = self.start()
        self.call("POST", f"collections/{c['id']}/otp/", {"otp": c["test_otp"]})
        status, body = self.call("POST", f"collections/{c['id']}/otp/", {"otp": c["test_otp"]})
        self.assertEqual(status, 200)                 # final state reported, not re-applied
        self.assertEqual(bal(BORROWER_PHONE), Decimal("380.00"))

    def test_unverified_lender_cannot_be_paid(self):
        self.register(party_ref="lender:2", merchant_code="LND-SUB")   # pending only
        status, body = self.start(payee_ref="lender:2", key="k2")
        self.assertEqual((status, body["error"]["code"]), (400, "merchant_not_verified"))

    def test_other_clients_cannot_see_or_complete_it(self):
        _, c = self.start()
        self.assertEqual(self.call("GET", f"collections/{c['id']}/", key=self.other_key)[0], 404)
        self.assertEqual(self.call("POST", f"collections/{c['id']}/otp/", {"otp": "1"},
                                   key=self.other_key)[0], 404)

    def test_validation(self):
        for overrides, code in [({"amount": "-5"}, "invalid_request"),
                                ({"amount": "abc"}, "invalid_request"),
                                ({"amount": "1.005"}, "invalid_request"),
                                ({"payer_account": ""}, "invalid_request"),
                                ({"provider": "nope"}, "unknown_provider")]:
            status, body = self.start(key=f"v{code}{overrides}", **overrides)
            self.assertEqual((status, body["error"]["code"]), (400, code), overrides)
