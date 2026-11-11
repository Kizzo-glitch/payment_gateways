"""Loads adapters listed in settings.PAYMENTS_ADAPTERS."""
from functools import lru_cache

from django.utils.module_loading import import_string

from .. import conf
from ..exceptions import PaymentError


@lru_cache(maxsize=None)
def _load():
    adapters = {}
    for code, path in conf.get("PAYMENTS_ADAPTERS").items():
        adapter = import_string(path)()
        if adapter.code != code:
            raise PaymentError(f"Adapter {path} has code {adapter.code!r}, settings say {code!r}")
        adapters[code] = adapter
    return adapters


def get_adapter(code: str):
    try:
        return _load()[code]
    except KeyError:
        raise PaymentError(f"No payment adapter registered for {code!r}") from None


def all_adapters():
    return dict(_load())


def reset():
    """For tests that change PAYMENTS_ADAPTERS."""
    _load.cache_clear()
