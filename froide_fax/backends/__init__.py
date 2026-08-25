from django.conf import settings
from django.utils.module_loading import import_string

from .base import BaseFaxBackend, FaxSendResult, SentFax, outbox

DEFAULT_FAX_BACKEND = "froide_fax.backends.telnyx.TelnyxFaxBackend"

__all__ = [
    "DEFAULT_FAX_BACKEND",
    "BaseFaxBackend",
    "FaxSendResult",
    "SentFax",
    "get_fax_backend",
    "outbox",
]


def get_fax_backend(path=None):
    """Instantiate the configured backend.

    Not cached: `override_settings(FAX_BACKEND=...)` has to take effect, the
    same way Django re-reads EMAIL_BACKEND per connection.
    """
    path = path or getattr(settings, "FAX_BACKEND", DEFAULT_FAX_BACKEND)
    return import_string(path)()
