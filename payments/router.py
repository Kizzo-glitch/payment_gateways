"""
Chooses a provider for a payment. Built on the same idea as Fedha-Grow's
payment-compatibility check: a provider is usable only if both parties have
that channel, the provider can do the job, and the settlement rules allow it.
Falls back to manual (off-platform, counterparty confirms).
"""
from dataclasses import dataclass, field

from . import conf, settlement
from .exceptions import (CapabilityNotSupported, MerchantNotConfigured, NoRouteAvailable,
                         PlatformCustodyNotPermitted)
from .providers.base import Capability, MerchantContext
from .providers.registry import all_adapters, get_adapter

MANUAL = "manual"


@dataclass
class Route:
    provider: str
    merchant: MerchantContext | None
    notes: list = field(default_factory=list)


def _channel(adapter):
    return getattr(adapter, "channel", adapter.code)


def choose(capability: Capability, merchant_party_ref: str, *, provider: str | None = None,
           payer_channels=None, payee_channels=None) -> Route:
    """merchant_party_ref is the lender: the payee for a collection, the
    payer for a disbursement."""
    if provider:  # explicit choice: errors surface, no silent fallback
        adapter = get_adapter(provider)
        adapter.require(capability)
        return Route(provider, settlement.resolve(provider, merchant_party_ref))

    adapters = all_adapters()
    order = [c for c in conf.get("PAYMENTS_PROVIDER_PREFERENCE") if c in adapters]
    order += [c for c in adapters if c not in order]
    notes = []

    for code in order:
        if code == MANUAL:
            continue
        adapter = adapters[code]
        if not adapter.supports(capability):
            continue
        channel = _channel(adapter)
        if payer_channels is not None and channel not in payer_channels:
            continue
        if payee_channels is not None and channel not in payee_channels:
            continue
        try:
            return Route(code, settlement.resolve(code, merchant_party_ref), notes)
        except (MerchantNotConfigured, CapabilityNotSupported, PlatformCustodyNotPermitted) as exc:
            notes.append(f"{code}: {exc}")

    if MANUAL in adapters and adapters[MANUAL].supports(capability):
        return Route(MANUAL, None, notes)
    raise NoRouteAvailable("; ".join(notes) or "No provider can make this payment")
