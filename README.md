# Fedha-Grow `payments` app

A drop-in Django app that moves money between borrowers and lenders through
mobile-money providers, without Fedha-Grow ever holding the funds.

This folder is a small Django project (`devproject/`) wrapped around the app
(`payments/`) so you can run the demo and tests on their own. Only the
`payments/` folder goes into Fedha-Grow.

## Try it

```bash
pip install "django>=4.2" cryptography requests
python manage.py migrate
python manage.py payments_demo      # the settlement-options demo
python manage.py test payments      # 67 tests
```

`payments_demo` uses only the simulator. It shows a borrower paying three
lenders, one per settlement option, with wallet balances after each step.

## The three settlement options

| | Who initiates | Whose credentials | Where the money lands | Status |
|---|---|---|---|---|
| **1. Direct** | Fedha-Grow | The lender's own merchant credentials | Lender's wallet | Works with C-Pay today, if each lender is a C-Pay merchant |
| **2. Sub-merchant** | Fedha-Grow | Fedha-Grow platform credentials | Lender's wallet (provider routes it) | Needs Chaperone to support sub-merchants |
| **3. Platform split** | Fedha-Grow | Fedha-Grow platform credentials | Fedha-Grow wallet, then paid on to the lender | **Off.** This is custody. Switch on only after CBL guidance |

Each lender's `MerchantAccount.settlement_mode` decides which option applies.
`payments/settlement.py` enforces the rules. Outside option 3, a lender's
money can never be set to settle into a Fedha-Grow merchant code, even if
the configuration is wrong.

## How it fits together

```
lending app ── PaymentService ──> router ──> settlement rules ──> adapter ──> provider
                     │                                                 │
                     └── intents, attempts, append-only events <───────┘
                                     │
                         reconciler + review queue
```

- **`services.PaymentService`** is the only thing lending code calls:
  `collect()`, `disburse()`, `submit_otp()`, `record_borrower_claim()`,
  `confirm_manual()`, `refresh()`, `handle_webhook()`.
- **`signals`**: lending code reacts to `payment_succeeded`, `payment_failed`,
  `payment_action_required` and `payment_needs_attention`. The payments app
  never imports lending code.
- **No foreign keys into lending.** Parties and loans are opaque strings
  (`"lender:12"`, `"repayment:4821"`), so the app can later become its own
  product (the aggregator idea) without a rewrite.
- **Records**: `PaymentIntent` (what should happen), `PaymentAttempt` (one try
  at one provider), `ProviderEvent` (raw evidence, append-only),
  `ReconciliationException` (things a person must look at).

## Adding EcoCash or M-Pesa

No rebuild is needed. The adapter contract already covers the way both of
them work (customer approves on the handset, result arrives by callback or
status query); the async simulator proves the rest of the app handles it.

1. Create `payments/providers/ecocash.py` with a class subclassing
   `ProviderAdapter`. Set `code = "ecocash"` and declare `capabilities`
   honestly. Implement `initiate_collection()` (return `Outcome.PENDING`),
   plus `query_status()` and/or `parse_webhook()`, and `initiate_disbursement()`
   if the API offers it.
2. Add one line to `PAYMENTS_ADAPTERS` in settings.
3. Add an `EcocashContract` subclass in `tests/test_adapter_contract.py`
   (about ten lines; there is a commented stub). The registry test fails until
   you do.
4. Create `MerchantAccount` rows for lenders at that provider.

Nothing in the service, router, settlement rules, jobs or webhook view
changes. Writing the adapter itself has to wait for real API documentation
and a sandbox; guessing at their APIs now would only produce code to throw away.

## Wiring into Fedha-Grow

1. Copy `payments/` into the project. If you already have an app named
   `payments` (for example the earlier simulator), merge or rename first.
2. Add `"payments"` to `INSTALLED_APPS` and copy the `PAYMENTS_*` and `CPAY`
   settings from `devproject/settings.py`. Generate a fresh
   `PAYMENTS_ENCRYPTION_KEY` and keep it in an environment variable.
3. `path("payments/", include("payments.urls"))` for webhooks.
4. Schedule the jobs (no Celery or Redis needed):
   - `python manage.py process_payments`: every minute
   - `python manage.py reconcile_payments`: daily
5. Connect lending to the signals, for example:

```python
from django.dispatch import receiver
from payments.signals import payment_succeeded

@receiver(payment_succeeded)
def settle_repayment(sender, intent, **kwargs):
    if intent.source_ref.startswith("repayment:"):
        ...  # mark the schedule item paid
```

6. Pass your existing payment-compatibility data to the router:
   `PaymentService.collect(..., payer_channels=[...], payee_channels=[...])`.
   With no shared channel it falls back to `manual` (borrower pays directly,
   lender confirms).

## C-Pay: what to confirm before production

The adapter follows the UAT docs (two endpoints, HMAC-SHA256 checksum with the
documented salt). Items marked `VERIFY` in `providers/cpay.py`:

- Exact response status strings, and the amount format inside the checksum
- Whether `LSL` is accepted as currency
- Authentication header (if any beyond the checksum)
- Hex or base64 checksum encoding

Settings flags to switch on only once Chaperone confirms:
`CPAY["SUPPORTS_SUB_MERCHANTS"]` (option 2) and
`CPAY["SUPPORTS_SPLIT_SETTLEMENT"]` (option 3). The field name for sending the
sub-merchant code (`SubMerchantCode`) is our proposal; agree the real one.

C-Pay has no status-query endpoint or webhooks. If a confirm call times out,
the payment goes to `unknown` and opens a review case rather than guessing.
