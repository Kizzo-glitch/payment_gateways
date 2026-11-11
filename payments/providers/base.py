"""
The provider adapter contract.

Every payment provider (C-Pay, EcoCash, M-Pesa, the simulator, manual EFT)
implements ProviderAdapter. The rest of the app only ever talks to this
interface, so adding a provider means writing one adapter file and adding one
line to settings.PAYMENTS_ADAPTERS.

Providers differ in how a customer approves a payment. The contract covers
all three patterns seen in Lesotho:
  - OTP confirm (C-Pay): initiate -> ACTION_REQUIRED("otp") -> submit_otp()
  - Async approval on the handset (M-Pesa STK push, EcoCash USSD push):
    initiate -> PENDING -> result arrives by webhook or query_status()
  - Off-platform with lender confirmation (manual EFT):
    initiate -> ACTION_REQUIRED("lender_confirmation") -> service confirms
"""
from abc import ABC
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from ..exceptions import CapabilityNotSupported


class Capability(str, Enum):
    COLLECT = "collect"                    # borrower pays a merchant
    DISBURSE = "disburse"                  # merchant pays a customer wallet
    STATUS_QUERY = "status_query"          # can ask "what happened to txn X?"
    WEBHOOKS = "webhooks"                  # provider pushes results to us
    REFUND = "refund"
    # Settlement models (see payments.models.SettlementMode)
    DIRECT_MERCHANT = "direct_merchant"    # option 1: act with lender's own credentials
    SUB_MERCHANTS = "sub_merchants"        # option 2: platform creds, settle to sub-merchant
    SPLIT_SETTLEMENT = "split_settlement"  # option 3: platform collects then pays out
    NEEDS_MERCHANT = "needs_merchant"      # payee must have a MerchantAccount here


class Outcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"                    # provider is processing; result comes later
    ACTION_REQUIRED = "action_required"    # someone must act (OTP, lender confirmation)
    UNKNOWN = "unknown"                    # we can't tell; must query or review


@dataclass(frozen=True)
class MerchantContext:
    """Who initiates, with which credentials, and where the money settles.
    Built by payments.settlement.resolve(); adapters must honour settles_to."""
    settlement_mode: str
    initiating_code: str          # merchant code whose credentials sign the call
    credentials: dict             # decrypted credentials of the initiator
    settles_to: str               # merchant code that ends up with the funds
    payee_ref: str


@dataclass(frozen=True)
class CollectionRequest:
    ext_transaction_id: str
    amount: Decimal
    currency: str
    payer_account: str            # borrower MSISDN / wallet
    reference: str
    narration: str
    merchant: MerchantContext | None


@dataclass(frozen=True)
class DisbursementRequest:
    ext_transaction_id: str
    amount: Decimal
    currency: str
    payee_account: str            # borrower MSISDN / wallet
    reference: str
    narration: str
    merchant: MerchantContext     # the lender paying out


@dataclass
class ProviderResponse:
    outcome: Outcome
    provider_transaction_id: str = ""
    action: str = ""              # "otp" | "lender_confirmation" when ACTION_REQUIRED
    error_code: str = ""
    message: str = ""
    amount: Decimal | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class WebhookResult:
    ext_transaction_id: str
    provider_transaction_id: str
    outcome: Outcome
    dedupe_key: str
    event_type: str = "payment.update"
    amount: Decimal | None = None
    raw: dict = field(default_factory=dict)


class ProviderAdapter(ABC):
    """Base class. Subclasses set `code` and `capabilities` and override the
    methods for the capabilities they declare. Undeclared ones raise."""

    code: str = ""
    display_name: str = ""
    capabilities: frozenset = frozenset()

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability):
        if not self.supports(capability):
            raise CapabilityNotSupported(f"{self.code} does not support {capability.value}")

    # --- money movement -------------------------------------------------
    def initiate_collection(self, req: CollectionRequest) -> ProviderResponse:
        raise CapabilityNotSupported(f"{self.code} does not support collections")

    def submit_otp(self, req: CollectionRequest, provider_transaction_id: str,
                   otp: str) -> ProviderResponse:
        raise CapabilityNotSupported(f"{self.code} does not use OTP confirmation")

    def initiate_disbursement(self, req: DisbursementRequest) -> ProviderResponse:
        raise CapabilityNotSupported(f"{self.code} does not support disbursements")

    # --- finding out what happened --------------------------------------
    def query_status(self, ext_transaction_id: str, provider_transaction_id: str,
                     merchant: MerchantContext | None) -> ProviderResponse:
        raise CapabilityNotSupported(f"{self.code} has no status query")

    def parse_webhook(self, headers: dict, body: bytes) -> WebhookResult:
        """Verify the signature and parse. Raise WebhookVerificationFailed if
        the signature is wrong."""
        raise CapabilityNotSupported(f"{self.code} does not send webhooks")
