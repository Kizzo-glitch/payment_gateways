"""Settings access with defaults, so the app works with minimal configuration."""
from django.conf import settings

DEFAULTS = {
    "PAYMENTS_PLATFORM_PARTY_REF": "platform:fedha-grow",
    "PAYMENTS_ALLOW_PLATFORM_SPLIT": False,
    "PAYMENTS_PROVIDER_PREFERENCE": ["cpay", "mpesa", "ecocash", "simulator", "manual"],
    "PAYMENTS_DEFAULT_CURRENCY": "LSL",
    "PAYMENTS_OTP_EXPIRY_MINUTES": 10,
    "PAYMENTS_POLL_INTERVAL_SECONDS": 60,
    "PAYMENTS_UNKNOWN_AFTER_MINUTES": 30,
    "PAYMENTS_CLAIM_STALE_DAYS": 3,
    "PAYMENTS_ADAPTERS": {
        "simulator": "payments.providers.simulator.SimulatorAdapter",
        "manual": "payments.providers.manual.ManualAdapter",
    },
}


def get(name):
    return getattr(settings, name, DEFAULTS.get(name))
