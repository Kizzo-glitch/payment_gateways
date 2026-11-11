from decimal import Decimal

from payments import demo
from payments.models import SimTransaction


def collect(n, payee_ref="lender:lesedi", amount="100.00", provider="simulator", **kw):
    from payments.services import PaymentService
    args = dict(source_ref=f"repayment:{n}", payer_ref="borrower:palesa", payee_ref=payee_ref,
                payer_account=demo.BORROWER_MSISDN, amount=Decimal(amount),
                reference=f"FG-{n}", idempotency_key=f"key:{n}", provider=provider)
    args.update(kw)
    return PaymentService.collect(**args)


def otp_for(intent):
    return SimTransaction.objects.get(
        ext_transaction_id=intent.current_attempt().ext_transaction_id).otp


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._data


class FakeSession:
    """Stands in for requests.Session in C-Pay tests."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
