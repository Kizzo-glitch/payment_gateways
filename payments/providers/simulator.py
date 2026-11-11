"""
Simulator adapters, backed by SimWallet balances in the database.

Two flavours, so both approval patterns are exercised before any real
provider is live:
  SimulatorAdapter       C-Pay style: initiate -> OTP -> confirm
  AsyncSimulatorAdapter  M-Pesa/EcoCash style: initiate -> customer approves
                         on handset -> result by webhook or status query

Both enforce the three settlement options exactly as we are asking the real
providers to, so the demo shows where money lands under each option.
"""
import hashlib
import hmac
import json
import random
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from ..exceptions import WebhookVerificationFailed
from ..models import SettlementMode, SimTransaction, SimWallet
from .base import (Capability, CollectionRequest, DisbursementRequest, MerchantContext,
                   Outcome, ProviderAdapter, ProviderResponse, WebhookResult)

OTP_SENT = "OTP_SENT"
PENDING_APPROVAL = "PENDING_APPROVAL"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


def _webhook_secret():
    return getattr(settings, "PAYMENTS_SIMULATOR_WEBHOOK_SECRET", "sim-webhook-secret")


def _fail(code, message, **raw):
    return ProviderResponse(Outcome.FAILED, error_code=code, message=message, raw=raw)


class _SimulatorBase(ProviderAdapter):
    capabilities = frozenset({
        Capability.COLLECT, Capability.DISBURSE, Capability.STATUS_QUERY,
        Capability.NEEDS_MERCHANT, Capability.DIRECT_MERCHANT,
        Capability.SUB_MERCHANTS, Capability.SPLIT_SETTLEMENT,
    })

    # --- checks the real provider would do ----------------------------
    def _authenticate(self, ctx: MerchantContext):
        wallet = SimWallet.objects.filter(code=ctx.initiating_code,
                                          kind=SimWallet.KIND_MERCHANT).first()
        creds = ctx.credentials or {}
        if (wallet is None or creds.get("client_code") != wallet.client_code
                or creds.get("client_secret") != wallet.client_secret):
            return None, _fail("AUTH_FAILED", "Merchant credentials rejected")
        return wallet, None

    def _route(self, ctx: MerchantContext):
        """Return (destination_code, via_code) or an error response."""
        dest = SimWallet.objects.filter(code=ctx.settles_to, kind=SimWallet.KIND_MERCHANT).first()
        if dest is None:
            return None, None, _fail("UNKNOWN_MERCHANT", f"No merchant {ctx.settles_to}")
        if ctx.settlement_mode == SettlementMode.DIRECT:
            if ctx.settles_to != ctx.initiating_code:
                return None, None, _fail(
                    "NOT_AUTHORISED", "Direct mode: can only settle to your own merchant code")
            return dest.code, "", None
        if ctx.settlement_mode == SettlementMode.SUB_MERCHANT:
            if dest.parent_code != ctx.initiating_code:
                return None, None, _fail(
                    "NOT_SUB_MERCHANT", f"{dest.code} is not a sub-merchant of {ctx.initiating_code}")
            return dest.code, "", None
        if ctx.settlement_mode == SettlementMode.PLATFORM_SPLIT:
            return dest.code, ctx.initiating_code, None
        return None, None, _fail("BAD_MODE", ctx.settlement_mode)

    @staticmethod
    def _move(from_code, to_code, amount, via_code=""):
        """Move money between wallets atomically. If via_code is set the
        funds pass through that wallet (option 3), leaving a visible trail."""
        codes = sorted({from_code, to_code, via_code} - {""})
        wallets = {w.code: w for w in SimWallet.objects.select_for_update().filter(code__in=codes)}
        src = wallets[from_code]
        if src.balance < amount:
            return False
        src.balance -= amount
        if via_code:
            wallets[via_code].balance += amount   # lands with Fedha-Grow...
            wallets[via_code].balance -= amount   # ...then is paid out
        wallets[to_code].balance += amount
        for w in wallets.values():
            w.save(update_fields=["balance"])
        return True

    def _new_txn(self, req, kind, from_code, to_code, via, status, otp=""):
        return SimTransaction.objects.create(
            ext_transaction_id=req.ext_transaction_id,
            provider_transaction_id=f"SIM-{uuid.uuid4().hex[:12].upper()}",
            kind=kind, amount=req.amount, from_code=from_code, to_code=to_code,
            settled_via=via, otp=otp, status=status)

    @staticmethod
    def _response_for(txn: SimTransaction) -> ProviderResponse:
        mapping = {COMPLETED: Outcome.SUCCEEDED, FAILED: Outcome.FAILED,
                   OTP_SENT: Outcome.ACTION_REQUIRED, PENDING_APPROVAL: Outcome.PENDING}
        return ProviderResponse(
            mapping[txn.status], txn.provider_transaction_id,
            action="otp" if txn.status == OTP_SENT else "",
            amount=txn.amount,
            raw={"status": txn.status, "to": txn.to_code, "via": txn.settled_via})

    def _validate_collection(self, req: CollectionRequest):
        if req.merchant is None:
            return None, None, _fail("NO_MERCHANT", "Merchant context required")
        _, err = self._authenticate(req.merchant)
        if err:
            return None, None, err
        payer = SimWallet.objects.filter(code=req.payer_account,
                                         kind=SimWallet.KIND_CUSTOMER).first()
        if payer is None:
            return None, None, _fail("UNKNOWN_PAYER", f"No wallet {req.payer_account}")
        dest, via, err = self._route(req.merchant)
        if err:
            return None, None, err
        return dest, via, None

    # --- disbursement (same for both flavours) -------------------------
    @transaction.atomic
    def initiate_disbursement(self, req: DisbursementRequest) -> ProviderResponse:
        _, err = self._authenticate(req.merchant)
        if err:
            return err
        source, via, err = self._route(req.merchant)   # lender's wallet is the source
        if err:
            return err
        if not SimWallet.objects.filter(code=req.payee_account,
                                        kind=SimWallet.KIND_CUSTOMER).exists():
            return _fail("UNKNOWN_PAYEE", f"No wallet {req.payee_account}")
        txn = self._new_txn(req, "disbursement", source, req.payee_account, via, FAILED)
        if self._move(source, req.payee_account, req.amount, via):
            txn.status = COMPLETED
            txn.save(update_fields=["status"])
            return self._response_for(txn)
        return _fail("INSUFFICIENT_FUNDS", "Lender wallet balance too low",
                     provider_transaction_id=txn.provider_transaction_id)

    def query_status(self, ext_transaction_id, provider_transaction_id, merchant):
        txn = SimTransaction.objects.filter(ext_transaction_id=ext_transaction_id).first()
        if txn is None:
            return ProviderResponse(Outcome.FAILED, error_code="NOT_FOUND",
                                    message="No such transaction")
        return self._response_for(txn)


class SimulatorAdapter(_SimulatorBase):
    """C-Pay-style OTP flow. The OTP is returned in raw['simulated_otp']
    (a real provider would send it to the customer's phone instead)."""
    code = "simulator"
    display_name = "Simulator (OTP flow)"

    @transaction.atomic
    def initiate_collection(self, req: CollectionRequest) -> ProviderResponse:
        dest, via, err = self._validate_collection(req)
        if err:
            return err
        otp = f"{random.randint(1000, 9999)}"
        txn = self._new_txn(req, "collection", req.payer_account, dest, via, OTP_SENT, otp)
        resp = self._response_for(txn)
        resp.raw["simulated_otp"] = otp
        return resp

    @transaction.atomic
    def submit_otp(self, req, provider_transaction_id, otp) -> ProviderResponse:
        txn = (SimTransaction.objects.select_for_update()
               .filter(ext_transaction_id=req.ext_transaction_id).first())
        if txn is None or txn.status != OTP_SENT:
            return _fail("INVALID_STATE", "Transaction is not awaiting an OTP")
        if otp != txn.otp:
            txn.status = FAILED
            txn.save(update_fields=["status"])
            return _fail("INVALID_OTP", "OTP does not match")
        if not self._move(txn.from_code, txn.to_code, txn.amount, txn.settled_via):
            txn.status = FAILED
            txn.save(update_fields=["status"])
            return _fail("INSUFFICIENT_FUNDS", "Customer balance too low")
        txn.status = COMPLETED
        txn.save(update_fields=["status"])
        return self._response_for(txn)


class AsyncSimulatorAdapter(_SimulatorBase):
    """M-Pesa / EcoCash-style flow: the customer approves on their handset and
    the result arrives later by webhook or status query."""
    code = "simulator_async"
    display_name = "Simulator (handset approval flow)"
    capabilities = _SimulatorBase.capabilities | {Capability.WEBHOOKS}

    @transaction.atomic
    def initiate_collection(self, req: CollectionRequest) -> ProviderResponse:
        dest, via, err = self._validate_collection(req)
        if err:
            return err
        txn = self._new_txn(req, "collection", req.payer_account, dest, via, PENDING_APPROVAL)
        return self._response_for(txn)

    # --- test/demo helpers: play the customer's handset ----------------
    @classmethod
    @transaction.atomic
    def customer_responds(cls, ext_transaction_id, approve=True):
        txn = SimTransaction.objects.select_for_update().get(ext_transaction_id=ext_transaction_id)
        if txn.status != PENDING_APPROVAL:
            return txn
        if approve and cls._move(txn.from_code, txn.to_code, txn.amount, txn.settled_via):
            txn.status = COMPLETED
        else:
            txn.status = FAILED
        txn.save(update_fields=["status"])
        return txn

    @classmethod
    def build_webhook(cls, ext_transaction_id):
        """Produce (headers, body) exactly as the provider would POST them."""
        txn = SimTransaction.objects.get(ext_transaction_id=ext_transaction_id)
        body = json.dumps({
            "ext_transaction_id": txn.ext_transaction_id,
            "transaction_id": txn.provider_transaction_id,
            "status": txn.status,
            "amount": str(txn.amount),
        }).encode()
        sig = hmac.new(_webhook_secret().encode(), body, hashlib.sha256).hexdigest()
        return {"X-Signature": sig}, body

    def parse_webhook(self, headers, body) -> WebhookResult:
        given = headers.get("X-Signature") or headers.get("HTTP_X_SIGNATURE") or ""
        expected = hmac.new(_webhook_secret().encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given, expected):
            raise WebhookVerificationFailed("Bad simulator webhook signature")
        data = json.loads(body)
        outcome = {COMPLETED: Outcome.SUCCEEDED, FAILED: Outcome.FAILED}.get(
            data["status"], Outcome.PENDING)
        return WebhookResult(
            ext_transaction_id=data["ext_transaction_id"],
            provider_transaction_id=data["transaction_id"],
            outcome=outcome,
            dedupe_key=f"{self.code}:{data['transaction_id']}:{data['status']}",
            amount=Decimal(data["amount"]),
            raw=data)
