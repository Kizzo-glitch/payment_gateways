class PaymentError(Exception):
    """Base class for payments errors."""


class InvalidTransition(PaymentError):
    pass


class CapabilityNotSupported(PaymentError):
    """The provider (or this provider's configuration) can't do this."""


class NoRouteAvailable(PaymentError):
    """No provider can move money between these parties."""


class MerchantNotConfigured(PaymentError):
    """The payee has no usable merchant account at this provider."""


class PlatformCustodyNotPermitted(PaymentError):
    """The settlement would put funds in a Fedha-Grow wallet (option 3),
    which is switched off. Fedha-Grow does not hold client funds."""


class IdempotencyConflict(PaymentError):
    """Same idempotency key reused with different payment details."""


class ProviderError(PaymentError):
    """A provider call failed in a way we understand."""

    def __init__(self, message, code="", retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ProviderTimeout(ProviderError):
    """We don't know whether the provider processed the request."""

    def __init__(self, message="Provider did not respond in time"):
        super().__init__(message, code="TIMEOUT", retryable=False)


class WebhookVerificationFailed(PaymentError):
    pass
