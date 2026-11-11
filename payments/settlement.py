"""
Decides who initiates a payment and where the money settles.

This is the one place that enforces Fedha-Grow's core regulatory position:
money owed to a lender never settles in a Fedha-Grow wallet, unless option 3
(PLATFORM_SPLIT) has been deliberately switched on after CBL guidance.

  Option 1 DIRECT        lender's own merchant credentials -> lender wallet
  Option 2 SUB_MERCHANT  Fedha-Grow platform credentials   -> lender wallet
  Option 3 PLATFORM_SPLIT Fedha-Grow credentials -> Fedha-Grow wallet -> lender
"""
from . import conf
from .exceptions import (CapabilityNotSupported, MerchantNotConfigured,
                         PlatformCustodyNotPermitted)
from .models import MerchantAccount, SettlementMode
from .providers.base import Capability, MerchantContext
from .providers.registry import get_adapter


def platform_ref():
    return conf.get("PAYMENTS_PLATFORM_PARTY_REF")


def platform_merchant_codes(provider_code):
    return set(MerchantAccount.objects.filter(
        provider=provider_code, party_ref=platform_ref()).values_list("merchant_code", flat=True))


def resolve(provider_code: str, payee_ref: str) -> MerchantContext | None:
    """Return the merchant context for paying `payee_ref` via `provider_code`.
    Returns None for providers that need no merchant account (manual EFT)."""
    adapter = get_adapter(provider_code)
    if not adapter.supports(Capability.NEEDS_MERCHANT):
        return None

    payee = (MerchantAccount.objects
             .filter(party_ref=payee_ref, provider=provider_code, is_active=True)
             .select_related("parent").first())
    if payee is None:
        raise MerchantNotConfigured(f"{payee_ref} has no active {provider_code} merchant account")

    mode = payee.settlement_mode

    if mode == SettlementMode.DIRECT:
        if not adapter.supports(Capability.DIRECT_MERCHANT):
            raise CapabilityNotSupported(f"{provider_code} cannot act for a lender's own merchant")
        if not payee.credentials_encrypted:
            raise MerchantNotConfigured(f"{payee} has no stored credentials (option 1 needs them)")
        ctx = MerchantContext(mode, payee.merchant_code, payee.credentials,
                              payee.merchant_code, payee_ref)

    elif mode == SettlementMode.SUB_MERCHANT:
        if not adapter.supports(Capability.SUB_MERCHANTS):
            raise CapabilityNotSupported(f"{provider_code} does not support sub-merchants yet")
        parent = payee.parent
        if parent is None or parent.party_ref != platform_ref() or not parent.is_active:
            raise MerchantNotConfigured(f"{payee} must sit under an active Fedha-Grow platform account")
        ctx = MerchantContext(mode, parent.merchant_code, parent.credentials,
                              payee.merchant_code, payee_ref)

    elif mode == SettlementMode.PLATFORM_SPLIT:
        if not conf.get("PAYMENTS_ALLOW_PLATFORM_SPLIT"):
            raise PlatformCustodyNotPermitted(
                "Option 3 would route lender funds through a Fedha-Grow wallet. "
                "It is disabled (PAYMENTS_ALLOW_PLATFORM_SPLIT=False).")
        if not adapter.supports(Capability.SPLIT_SETTLEMENT):
            raise CapabilityNotSupported(f"{provider_code} does not support split settlement")
        platform = (payee.parent if payee.parent and payee.parent.party_ref == platform_ref()
                    else MerchantAccount.objects.filter(provider=provider_code,
                                                        party_ref=platform_ref(),
                                                        is_active=True).first())
        if platform is None:
            raise MerchantNotConfigured("No Fedha-Grow platform account at this provider")
        ctx = MerchantContext(mode, platform.merchant_code, platform.credentials,
                              payee.merchant_code, payee_ref)
    else:
        raise MerchantNotConfigured(f"Unknown settlement mode {mode!r}")

    _guard_no_custody(provider_code, ctx)
    return ctx


def _guard_no_custody(provider_code, ctx: MerchantContext):
    """Belt and braces: outside option 3, a lender's money may never be set to
    settle into a Fedha-Grow merchant code. (Fedha-Grow collecting its own
    subscription fees, where the platform IS the payee, is not custody.)"""
    if ctx.payee_ref == platform_ref() or ctx.settlement_mode == SettlementMode.PLATFORM_SPLIT:
        return
    if ctx.settles_to in platform_merchant_codes(provider_code):
        raise PlatformCustodyNotPermitted(
            f"Funds for {ctx.payee_ref} would settle to Fedha-Grow code {ctx.settles_to}")
