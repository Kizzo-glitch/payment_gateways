"""
Demo data and scenarios for showing payment partners what we mean by each
settlement option. Uses only the simulator; touches no real provider.
"""
from decimal import Decimal

from django.test.utils import override_settings

from . import conf
from .exceptions import PlatformCustodyNotPermitted
from .models import (MerchantAccount, PaymentAttempt, PaymentIntent, ProviderEvent,
                     ReconciliationException, SettlementMode, SimTransaction, SimWallet)
from .providers.registry import get_adapter
from .providers.simulator import AsyncSimulatorAdapter
from .services import PaymentService

BORROWER_MSISDN = "26658001111"
PLATFORM_CODE = "FG-PLATFORM"
LENDERS = {
    # party_ref: (merchant code, name, mode, own credentials?)
    "lender:lesedi": ("LND-LESEDI", "Lesedi Microloans", SettlementMode.DIRECT, True),
    "lender:thaba": ("LND-THABA", "Thaba Finance", SettlementMode.SUB_MERCHANT, False),
    "lender:maluti": ("LND-MALUTI", "Maluti Lending", SettlementMode.PLATFORM_SPLIT, False),
}
SIM_PROVIDERS = ("simulator", "simulator_async")


def reset():
    sim_intents = PaymentIntent.objects.filter(provider__in=SIM_PROVIDERS)
    ReconciliationException.objects.filter(intent__in=sim_intents).delete()
    ReconciliationException.objects.filter(event__provider__in=SIM_PROVIDERS).delete()
    ProviderEvent.objects.filter(provider__in=SIM_PROVIDERS).delete()
    PaymentAttempt.objects.filter(intent__in=sim_intents).delete()
    sim_intents.delete()
    MerchantAccount.objects.filter(provider__in=SIM_PROVIDERS, parent__isnull=False).delete()
    MerchantAccount.objects.filter(provider__in=SIM_PROVIDERS).delete()
    SimTransaction.objects.all().delete()
    SimWallet.objects.all().delete()


def seed():
    reset()
    SimWallet.objects.create(code=BORROWER_MSISDN, kind=SimWallet.KIND_CUSTOMER,
                             label="Borrower (Palesa)", balance=Decimal("2000.00"))
    SimWallet.objects.create(code=PLATFORM_CODE, kind=SimWallet.KIND_MERCHANT,
                             label="Fedha-Grow platform", balance=Decimal("0.00"),
                             client_code="FG8970", client_secret="fg-secret")
    for provider in SIM_PROVIDERS:
        platform = MerchantAccount(party_ref=conf.get("PAYMENTS_PLATFORM_PARTY_REF"),
                                   provider=provider, merchant_code=PLATFORM_CODE,
                                   display_name="Fedha-Grow platform")
        platform.set_credentials({"client_code": "FG8970", "client_secret": "fg-secret"})
        platform.save()
        for party_ref, (code, name, mode, own_creds) in LENDERS.items():
            if provider == SIM_PROVIDERS[0]:
                SimWallet.objects.create(
                    code=code, kind=SimWallet.KIND_MERCHANT, label=name,
                    balance=Decimal("5000.00"),
                    client_code=f"C-{code}" if own_creds else "",
                    client_secret=f"{code}-secret" if own_creds else "",
                    parent_code="" if own_creds else PLATFORM_CODE)
            m = MerchantAccount(party_ref=party_ref, provider=provider, merchant_code=code,
                                display_name=name, settlement_mode=mode,
                                parent=None if own_creds else platform)
            if own_creds:
                m.set_credentials({"client_code": f"C-{code}", "client_secret": f"{code}-secret"})
            m.save()


def balances():
    return {w.label: w.balance for w in SimWallet.objects.order_by("id")}


def _collect(n, payee_ref, amount, provider="simulator"):
    return PaymentService.collect(
        source_ref=f"repayment:demo-{n}", payer_ref="borrower:palesa", payee_ref=payee_ref,
        payer_account=BORROWER_MSISDN, amount=Decimal(amount), reference=f"FG-DEMO-{n}",
        idempotency_key=f"demo:{n}", provider=provider)


def _otp_for(intent):
    return SimTransaction.objects.get(
        ext_transaction_id=intent.current_attempt().ext_transaction_id).otp


def run(out=print):
    seed()
    start = balances()

    def show(title, before):
        out("")
        out(title)
        now = balances()
        for label, bal in now.items():
            delta = bal - before[label]
            mark = f"  ({'+' if delta > 0 else ''}{delta})" if delta else ""
            out(f"    {label:<22} M{bal:>9}{mark}")
        return now

    out("=" * 66)
    out("Fedha-Grow payment settlement demo (simulator only)")
    out("=" * 66)
    show("Starting balances", start)

    # Option 1
    before = balances()
    i1 = _collect(1, "lender:lesedi", "450.00")
    out(f"\nOption 1 - DIRECT. Fedha-Grow initiates with Lesedi's OWN merchant "
        f"credentials.\n  Borrower receives OTP; status: {i1.status} ({i1.required_action})")
    i1 = PaymentService.submit_otp(i1, _otp_for(i1))
    out(f"  OTP entered -> {i1.status}; settled to {i1.settles_to}")
    show("  Balances after option 1:", before)

    # Option 2
    before = balances()
    i2 = _collect(2, "lender:thaba", "300.00")
    i2 = PaymentService.submit_otp(i2, _otp_for(i2))
    out(f"\nOption 2 - SUB-MERCHANT. Fedha-Grow initiates with its PLATFORM "
        f"credentials;\n  Thaba is a sub-merchant, so the provider settles to Thaba. "
        f"-> {i2.status}, settled to {i2.settles_to}")
    show("  Balances after option 2:", before)

    # Option 3, switched off
    out("\nOption 3 - PLATFORM SPLIT, with the safety switch OFF (default):")
    try:
        _collect(3, "lender:maluti", "200.00")
    except PlatformCustodyNotPermitted as exc:
        out(f"  Blocked: {exc}")

    # Option 3, illustration only
    before = balances()
    with override_settings(PAYMENTS_ALLOW_PLATFORM_SPLIT=True):
        i3 = _collect(4, "lender:maluti", "200.00")
        i3 = PaymentService.submit_otp(i3, _otp_for(i3))
    txn = SimTransaction.objects.get(ext_transaction_id=i3.current_attempt().ext_transaction_id)
    out(f"\nOption 3 - illustration only (switch forced ON for this one payment):\n"
        f"  -> {i3.status}. Money moved {txn.from_code} -> {txn.settled_via} -> {txn.to_code}.\n"
        f"  It passed THROUGH the Fedha-Grow wallet. That is custody, which is why it is off.")
    show("  Balances after option 3:", before)

    # Async flavour (how M-Pesa / EcoCash will behave)
    before = balances()
    i5 = _collect(5, "lender:lesedi", "100.00", provider="simulator_async")
    out(f"\nHandset-approval flow (how M-Pesa/EcoCash behave), option 1:\n"
        f"  initiated -> {i5.status} (borrower approves on phone)")
    ext = i5.current_attempt().ext_transaction_id
    AsyncSimulatorAdapter.customer_responds(ext, approve=True)
    headers, body = AsyncSimulatorAdapter.build_webhook(ext)
    PaymentService.handle_webhook("simulator_async", headers, body)
    dup = PaymentService.handle_webhook("simulator_async", headers, body)
    i5.refresh_from_db()
    out(f"  webhook received -> {i5.status}; same webhook again -> "
        f"{'ignored (duplicate)' if dup is None else 'applied twice!'}")
    show("  Balances after handset flow:", before)

    # Disbursement
    before = balances()
    d = PaymentService.disburse(
        source_ref="loan:demo-6", payer_ref="lender:thaba", payee_ref="borrower:palesa",
        payee_account=BORROWER_MSISDN, amount=Decimal("1000.00"), reference="FG-DISB-6",
        idempotency_key="demo:6", provider="simulator")
    out(f"\nDisbursement, option 2: Thaba pays the borrower from Thaba's own wallet "
        f"-> {d.status}")
    show("  Balances after disbursement:", before)

    out("\nFedha-Grow platform wallet at the end: "
        f"M{SimWallet.objects.get(code=PLATFORM_CODE).balance} "
        "(it never held lender funds in options 1 and 2).")
    events = ProviderEvent.objects.filter(provider__in=SIM_PROVIDERS).count()
    out(f"Evidence trail: {events} provider events recorded, append-only.")
    return {"intents": [i1, i2, i3, i5, d]}
