"""
Payments data model.

Design rules:
- No foreign keys into the lending app. Loans, lenders and borrowers are
  referenced by opaque strings (source_ref, payer_ref, payee_ref) such as
  "repayment:4821" or "lender:12". This keeps the app extractable.
- Intent = what should happen. Attempt = one try at one provider.
  ProviderEvent = raw evidence from a provider, stored before interpretation.
- Money never settles to Fedha-Grow unless settlement mode PLATFORM_SPLIT
  (option 3) is explicitly allowed in settings.
"""
import hashlib
import secrets
import uuid

from django.db import models
from django.utils import timezone

from . import crypto


class ApiClient(models.Model):
    """
    An application allowed to call the gateway's API (Fedha-Grow is the first).
    Only a SHA-256 hash of the key is stored; the key itself is shown once,
    when the client is created (manage.py create_api_client).
    """

    KEY_PREFIX = "pgk_"

    name = models.CharField(max_length=120, unique=True)
    key_prefix = models.CharField(max_length=16, help_text="First characters, to recognise a key.")
    key_hash = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.key_prefix}…)"

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    @classmethod
    def create_with_key(cls, name):
        """Create a client and return (client, raw_key). Store the key now:
        it can't be recovered later."""
        raw = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        client = cls.objects.create(name=name, key_prefix=raw[:12], key_hash=cls.hash_key(raw))
        return client, raw

    def rotate_key(self):
        raw = self.KEY_PREFIX + secrets.token_urlsafe(32)
        self.key_prefix, self.key_hash = raw[:12], self.hash_key(raw)
        self.save(update_fields=["key_prefix", "key_hash"])
        return raw

    @classmethod
    def authenticate(cls, raw_key: str):
        if not raw_key or not raw_key.startswith(cls.KEY_PREFIX):
            return None
        return cls.objects.filter(key_hash=cls.hash_key(raw_key), is_active=True).first()


class MerchantStatus(models.TextChoices):
    PENDING = "pending_verification", "Pending verification"
    VERIFIED = "verified", "Verified"
    FAILED = "failed", "Verification failed"
    DISABLED = "disabled", "Disabled"


class SettlementMode(models.TextChoices):
    # Option 1: lender is its own merchant; Fedha-Grow initiates using the
    # lender's own merchant credentials. Funds settle in the lender's wallet.
    DIRECT = "direct", "Direct (lender's own merchant account)"
    # Option 2: lender is a sub-merchant under a Fedha-Grow platform account.
    # Fedha-Grow initiates with platform credentials; the provider settles
    # funds to the sub-merchant (lender) wallet.
    SUB_MERCHANT = "sub_merchant", "Sub-merchant under Fedha-Grow platform"
    # Option 3 (kept in mind, disabled by default): funds land in the
    # Fedha-Grow wallet, then get paid out to the lender. This is custody.
    PLATFORM_SPLIT = "platform_split", "Platform collects then splits (custody)"


class MerchantAccount(models.Model):
    """A merchant identity at one provider. Fedha-Grow's own platform account
    is one of these; each lender has one per provider it uses."""

    client = models.ForeignKey(ApiClient, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="merchants",
                               help_text="The API client that registered this merchant.")
    party_ref = models.CharField(max_length=64, db_index=True,
                                 help_text='e.g. "lender:12", or "platform" for the client itself')
    provider = models.CharField(max_length=32)
    merchant_code = models.CharField(max_length=64)
    display_name = models.CharField(max_length=120, blank=True)
    settlement_mode = models.CharField(max_length=20, choices=SettlementMode.choices,
                                       default=SettlementMode.DIRECT)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="sub_merchants",
                               help_text="Platform account, for sub-merchants (option 2).")
    credentials_encrypted = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    status = models.CharField(max_length=24, choices=MerchantStatus.choices,
                              default=MerchantStatus.PENDING, db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True)
    verification_intent = models.ForeignKey("PaymentIntent", null=True, blank=True,
                                            on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["client", "provider", "merchant_code"],
                                    name="uniq_client_provider_merchant_code"),
            models.UniqueConstraint(fields=["client", "party_ref", "provider"],
                                    name="uniq_client_party_provider"),
        ]

    @property
    def is_usable(self):
        return self.is_active and self.status == MerchantStatus.VERIFIED

    def __str__(self):
        return f"{self.display_name or self.party_ref} @ {self.provider} ({self.merchant_code})"

    @property
    def credentials(self) -> dict:
        return crypto.decrypt_dict(self.credentials_encrypted)

    def set_credentials(self, data: dict):
        self.credentials_encrypted = crypto.encrypt_dict(data)


class PaymentKind(models.TextChoices):
    COLLECTION = "collection", "Collection (borrower pays lender)"
    DISBURSEMENT = "disbursement", "Disbursement (lender pays borrower)"


class IntentStatus(models.TextChoices):
    CREATED = "created", "Created"
    PENDING = "pending", "Pending at provider"
    AWAITING_ACTION = "awaiting_action", "Awaiting customer or lender action"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled"
    UNKNOWN = "unknown", "Unknown (needs status check or review)"


class PaymentIntent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(ApiClient, null=True, blank=True, on_delete=models.PROTECT,
                               related_name="intents")
    kind = models.CharField(max_length=16, choices=PaymentKind.choices)
    source_ref = models.CharField(max_length=64, db_index=True,
                                  help_text='What this pays for, e.g. "repayment:4821".')
    payer_ref = models.CharField(max_length=64)
    payee_ref = models.CharField(max_length=64)
    payer_account = models.CharField(max_length=64, blank=True,
                                     help_text="Payer wallet/MSISDN for collections.")
    payee_account = models.CharField(max_length=64, blank=True,
                                     help_text="Payee wallet/MSISDN for disbursements.")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="LSL")
    reference = models.CharField(max_length=64, help_text="Human-readable payment reference.")
    narration = models.CharField(max_length=140, blank=True)
    idempotency_key = models.CharField(max_length=128, unique=True)
    provider = models.CharField(max_length=32)
    merchant = models.ForeignKey(MerchantAccount, null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="intents")
    settlement_mode = models.CharField(max_length=20, choices=SettlementMode.choices, blank=True)
    settles_to = models.CharField(max_length=64, blank=True,
                                  help_text="Merchant code the funds settle to (audit snapshot).")
    status = models.CharField(max_length=20, choices=IntentStatus.choices,
                              default=IntentStatus.CREATED, db_index=True)
    required_action = models.CharField(max_length=32, blank=True,
                                       help_text='e.g. "otp" or "lender_confirmation".')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind} {self.amount} {self.currency} {self.reference} [{self.status}]"

    @property
    def is_final(self):
        return self.status in {IntentStatus.SUCCEEDED, IntentStatus.FAILED, IntentStatus.CANCELLED}

    def current_attempt(self):
        return self.attempts.order_by("-created_at").first()


class AttemptStatus(models.TextChoices):
    INITIATED = "initiated", "Initiated"
    AWAITING_OTP = "awaiting_otp", "Awaiting OTP"
    AWAITING_CONFIRMATION = "awaiting_confirmation", "Awaiting lender confirmation"
    PROCESSING = "processing", "Processing at provider"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"
    EXPIRED = "expired", "Expired"
    UNKNOWN = "unknown", "Unknown"


class PaymentAttempt(models.Model):
    intent = models.ForeignKey(PaymentIntent, on_delete=models.CASCADE, related_name="attempts")
    provider = models.CharField(max_length=32)
    ext_transaction_id = models.CharField(max_length=64, unique=True,
                                          help_text="Our ID sent to the provider.")
    provider_transaction_id = models.CharField(max_length=128, blank=True, db_index=True)
    status = models.CharField(max_length=24, choices=AttemptStatus.choices,
                              default=AttemptStatus.INITIATED, db_index=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)

    def __str__(self):
        return f"{self.provider}:{self.ext_transaction_id} [{self.status}]"


class EventSource(models.TextChoices):
    SYNC_RESPONSE = "sync_response", "Synchronous API response"
    WEBHOOK = "webhook", "Webhook / callback"
    POLL = "poll", "Status query"
    BORROWER_CLAIM = "borrower_claim", "Borrower claim"
    LENDER_CONFIRMATION = "lender_confirmation", "Lender confirmation"
    SYSTEM = "system", "System"


class ProviderEvent(models.Model):
    """Append-only evidence log. Never update or delete rows."""

    provider = models.CharField(max_length=32)
    attempt = models.ForeignKey(PaymentAttempt, null=True, blank=True,
                                on_delete=models.PROTECT, related_name="events")
    source = models.CharField(max_length=24, choices=EventSource.choices)
    event_type = models.CharField(max_length=64)
    dedupe_key = models.CharField(max_length=191, unique=True)
    payload = models.JSONField(default=dict)
    actor_ref = models.CharField(max_length=64, blank=True)
    received_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["received_at"]

    def __str__(self):
        return f"{self.provider} {self.source} {self.event_type}"


class ExceptionKind(models.TextChoices):
    UNKNOWN_OUTCOME = "unknown_outcome", "Outcome unknown after timeout"
    STALE_CLAIM = "stale_claim", "Borrower claim not confirmed by lender"
    UNMATCHED_EVENT = "unmatched_event", "Provider event matches no attempt"
    AMOUNT_MISMATCH = "amount_mismatch", "Provider amount differs from intent"
    SUCCESS_WITHOUT_EVIDENCE = "success_without_evidence", "Succeeded with no provider evidence"
    CLAIM_DISPUTED = "claim_disputed", "Borrower claims paid; lender says not received"


class ReconciliationException(models.Model):
    kind = models.CharField(max_length=32, choices=ExceptionKind.choices)
    intent = models.ForeignKey(PaymentIntent, null=True, blank=True,
                               on_delete=models.CASCADE, related_name="exceptions")
    event = models.ForeignKey(ProviderEvent, null=True, blank=True, on_delete=models.PROTECT)
    detail = models.TextField(blank=True)
    is_resolved = models.BooleanField(default=False)
    resolution_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "intent"],
                                    condition=models.Q(is_resolved=False),
                                    name="one_open_exception_per_kind_per_intent"),
        ]

    def __str__(self):
        return f"{self.kind} ({'resolved' if self.is_resolved else 'open'})"


# ---------------------------------------------------------------------------
# Simulator state. Used only by the simulator adapters, for partner testing
# and demos. Wallets have real balances so you can show where money lands.
# ---------------------------------------------------------------------------

class SimWallet(models.Model):
    KIND_CUSTOMER = "customer"
    KIND_MERCHANT = "merchant"

    code = models.CharField(max_length=64, unique=True, help_text="MSISDN or merchant code.")
    kind = models.CharField(max_length=16, choices=[(KIND_CUSTOMER, "Customer"),
                                                    (KIND_MERCHANT, "Merchant")])
    label = models.CharField(max_length=120)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Merchant-only fields
    client_code = models.CharField(max_length=64, blank=True)
    client_secret = models.CharField(max_length=128, blank=True)
    parent_code = models.CharField(max_length=64, blank=True,
                                   help_text="Platform merchant code, for sub-merchants.")

    def __str__(self):
        return f"{self.label} ({self.code}): M{self.balance}"


class SimTransaction(models.Model):
    ext_transaction_id = models.CharField(max_length=64, unique=True)
    provider_transaction_id = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    from_code = models.CharField(max_length=64)
    to_code = models.CharField(max_length=64)
    settled_via = models.CharField(max_length=64, blank=True,
                                   help_text="Intermediate wallet, if funds passed through one.")
    otp = models.CharField(max_length=8, blank=True)
    status = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
