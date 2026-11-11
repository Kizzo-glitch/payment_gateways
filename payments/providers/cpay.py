"""
C-Pay (Chaperone) adapter, written against the UAT documentation:

  POST {BASE_URL}/api/cpaypayments/payment   initiate; OTP sent to customer
  POST {BASE_URL}/api/cpaypayments/confirm   confirm with OTP; synchronous result
  Checksum = HMAC-SHA256(client_secret, salt)
    initiate salt: ExtTransactionId + ClientCode + Amount + MSISDN
    confirm  salt: ExtTransactionId + ClientCode + Amount + MSISDN + OTP

What C-Pay does NOT offer yet (per the docs), so this adapter does not
claim it: disbursement, status query, webhooks. Sub-merchant and split
settlement are switched on per settings once Chaperone confirms them.

Items to verify against the docs or with Chaperone before production are
marked VERIFY.
"""
import hashlib
import hmac
import base64
from decimal import Decimal

import requests
from django.conf import settings

from ..exceptions import ProviderTimeout
from ..models import SettlementMode
from .base import (Capability, CollectionRequest, Outcome, ProviderAdapter,
                   ProviderResponse)

# VERIFY: exact status strings returned by C-Pay.
SUCCESS_STATUSES = {"SUCCESS", "SUCCESSFUL", "COMPLETED", "APPROVED", "00"}
OTP_STATUSES = {"OTP_SENT", "PENDING", "PENDING_OTP", "AWAITING_OTP"}
FAILED_STATUSES = {"FAILED", "DECLINED", "REJECTED", "INVALID_OTP", "INSUFFICIENT_FUNDS", "ERROR"}


def _cfg():
    defaults = {
        "BASE_URL": "https://cpay-uat-env.chaperone.co.ls/chaperone-apihub",
        "PAYMENT_PATH": "/api/cpaypayments/payment",
        "CONFIRM_PATH": "/api/cpaypayments/confirm",
        "TIMEOUT_SECONDS": 20,
        "CHECKSUM_ENCODING": "hex",
        "SUPPORTS_SUB_MERCHANTS": False,
        "SUPPORTS_SPLIT_SETTLEMENT": False,
        # Proposed field name for option 2; agree the real one with Chaperone.
        "SUB_MERCHANT_FIELD": "SubMerchantCode",
    }
    return {**defaults, **getattr(settings, "CPAY", {})}


def format_amount(amount: Decimal) -> str:
    # VERIFY: amount format used inside the checksum ("150.00" vs "150").
    return f"{Decimal(amount):.2f}"


def checksum(secret: str, *parts: str, encoding: str = "hex") -> str:
    digest = hmac.new(secret.encode(), "".join(parts).encode(), hashlib.sha256)
    return base64.b64encode(digest.digest()).decode() if encoding == "base64" else digest.hexdigest()


class CpayAdapter(ProviderAdapter):
    code = "cpay"
    display_name = "C-Pay (Chaperone)"

    def __init__(self, session=None):
        self.cfg = _cfg()
        self.session = session or requests.Session()
        caps = {Capability.COLLECT, Capability.NEEDS_MERCHANT, Capability.DIRECT_MERCHANT}
        if self.cfg["SUPPORTS_SUB_MERCHANTS"]:
            caps.add(Capability.SUB_MERCHANTS)
        if self.cfg["SUPPORTS_SPLIT_SETTLEMENT"]:
            caps.add(Capability.SPLIT_SETTLEMENT)
        self.capabilities = frozenset(caps)

    # --- helpers -------------------------------------------------------
    def _body(self, req: CollectionRequest, otp: str = "") -> dict:
        ctx = req.merchant
        creds = ctx.credentials
        client_code = creds["client_code"]
        amount = format_amount(req.amount)
        salt = [req.ext_transaction_id, client_code, amount, req.payer_account]
        if otp:
            salt.append(otp)
        body = {
            "ExtTransactionId": req.ext_transaction_id,
            "ClientCode": client_code,
            "MerchantCode": ctx.initiating_code,
            "MSISDN": req.payer_account,
            "Amount": amount,
            "Currency": req.currency,         # VERIFY: LSL accepted?
            "Narration": req.narration or req.reference,
            "Checksum": checksum(creds["client_secret"], *salt,
                                 encoding=self.cfg["CHECKSUM_ENCODING"]),
        }
        if otp:
            body["OTP"] = otp
        if ctx.settlement_mode in (SettlementMode.SUB_MERCHANT, SettlementMode.PLATFORM_SPLIT):
            body[self.cfg["SUB_MERCHANT_FIELD"]] = ctx.settles_to
        return body

    def _headers(self, req: CollectionRequest) -> dict:
        headers = {"Content-Type": "application/json"}
        api_key = req.merchant.credentials.get("api_key")
        if api_key:  # VERIFY: auth header name/format
            headers["Authorization"] = api_key
        return headers

    def _post(self, path, req, body):
        try:
            resp = self.session.post(self.cfg["BASE_URL"] + path, json=body,
                                     headers=self._headers(req),
                                     timeout=self.cfg["TIMEOUT_SECONDS"])
        except (requests.Timeout, requests.ConnectionError) as exc:
            # We sent it; we don't know if C-Pay processed it.
            raise ProviderTimeout(str(exc)) from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"_http_status": resp.status_code, "_text": resp.text[:500]}
        data.setdefault("_http_status", resp.status_code)
        return data

    @staticmethod
    def _status(data):
        return str(data.get("Status") or data.get("status") or "").upper()

    @staticmethod
    def _txn_id(data):
        return str(data.get("TransactionId") or data.get("transactionId") or "")

    # --- contract ------------------------------------------------------
    def initiate_collection(self, req: CollectionRequest) -> ProviderResponse:
        data = self._post(self.cfg["PAYMENT_PATH"], req, self._body(req))
        status = self._status(data)
        if status in OTP_STATUSES:
            return ProviderResponse(Outcome.ACTION_REQUIRED, self._txn_id(data), action="otp",
                                    message=data.get("Message", ""), raw=data)
        if status in FAILED_STATUSES or data["_http_status"] >= 400:
            return ProviderResponse(Outcome.FAILED, self._txn_id(data),
                                    error_code=str(data.get("ErrorCode") or status),
                                    message=data.get("Message", ""), raw=data)
        return ProviderResponse(Outcome.UNKNOWN, self._txn_id(data),
                                message=f"Unrecognised C-Pay status {status!r}", raw=data)

    def submit_otp(self, req, provider_transaction_id, otp) -> ProviderResponse:
        data = self._post(self.cfg["CONFIRM_PATH"], req, self._body(req, otp=otp))
        status = self._status(data)
        txn_id = self._txn_id(data) or provider_transaction_id
        if status in SUCCESS_STATUSES:
            return ProviderResponse(Outcome.SUCCEEDED, txn_id, raw=data)
        if status in FAILED_STATUSES or data["_http_status"] >= 400:
            return ProviderResponse(Outcome.FAILED, txn_id,
                                    error_code=str(data.get("ErrorCode") or status),
                                    message=data.get("Message", ""), raw=data)
        # C-Pay has no status-query endpoint, so an unrecognised answer goes
        # to UNKNOWN and the reconciler raises it for manual review.
        return ProviderResponse(Outcome.UNKNOWN, txn_id,
                                message=f"Unrecognised C-Pay status {status!r}", raw=data)
