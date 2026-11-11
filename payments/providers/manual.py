"""
Manual / EFT adapter: money moves outside the platform (bank transfer, cash
deposit, a wallet we're not integrated with). Fedha-Grow records the payment
and the counterparty confirms it. A borrower's claim alone never settles it.
"""
from .base import (Capability, CollectionRequest, DisbursementRequest, Outcome,
                   ProviderAdapter, ProviderResponse)


class ManualAdapter(ProviderAdapter):
    code = "manual"
    display_name = "Manual / EFT (counterparty confirms)"
    capabilities = frozenset({Capability.COLLECT, Capability.DISBURSE})

    def initiate_collection(self, req: CollectionRequest) -> ProviderResponse:
        return ProviderResponse(Outcome.ACTION_REQUIRED, action="lender_confirmation",
                                message="Borrower pays the lender directly; lender confirms receipt.")

    def initiate_disbursement(self, req: DisbursementRequest) -> ProviderResponse:
        return ProviderResponse(Outcome.ACTION_REQUIRED, action="recipient_confirmation",
                                message="Lender pays the borrower directly; borrower confirms receipt.")
