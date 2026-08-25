"""Fax transport backends.

Mirrors Django's EMAIL_BACKEND: `settings.FAX_BACKEND` names a class, and
everything that sends goes through it. The point is to be able to exercise
message creation, PDF rendering, attachment storage and status handling without
touching Telnyx -- and without each caller growing an `if DEBUG` branch.

The two seams are `froide_fax.fax.send_fax` and `froide_fax.fax.get_fax`.
"""

import dataclasses
import datetime


@dataclasses.dataclass
class FaxSendResult:
    """What a backend reports back about an accepted fax.

    Deliberately not a `requests.Response`: a backend that never talks HTTP
    should not have to fake one, and the caller only ever wanted these two
    values.
    """

    fax_id: str = ""
    accepted: bool = False


@dataclasses.dataclass
class SentFax:
    """One fax a recording backend was asked to send."""

    to: str
    media_url: str
    fax_id: str
    timestamp: datetime.datetime


# Mirrors django.core.mail.outbox. Recording backends append here; tests assert
# against it. Not thread-safe, and not meant for production use.
outbox: list = []


class BaseFaxBackend:
    def send(self, to: str, media_url: str) -> FaxSendResult:
        raise NotImplementedError

    def get_status(self, fax_id: str) -> dict | None:
        """Return a Telnyx-shaped fax object, or None if unknown.

        Shape matters: `poll_fax_status` reads `status` off this and passes the
        whole thing to `fax_log_from_api`.
        """
        raise NotImplementedError


class RecordingFaxBackend(BaseFaxBackend):
    """Accepts every fax, remembers it, and reports it delivered.

    `get_status` answers from the outbox so the polling sweep can resolve a
    message end to end. Without that, a fax sent by a non-delivering backend
    would sit in STATUS_SENDING for ever, since no webhook is ever going to
    arrive.
    """

    def send(self, to, media_url):
        from django.utils import timezone

        fax_id = "dummy-%s" % (len(outbox) + 1)
        sent = SentFax(
            to=to, media_url=media_url, fax_id=fax_id, timestamp=timezone.now()
        )
        outbox.append(sent)
        self.record(sent)
        return FaxSendResult(fax_id=fax_id, accepted=True)

    def record(self, sent: SentFax) -> None:
        """Hook for subclasses that also want to report the fax somewhere."""

    def get_status(self, fax_id):
        for sent in outbox:
            if sent.fax_id == fax_id:
                return {
                    "id": sent.fax_id,
                    "record_type": "fax",
                    "direction": "outbound",
                    "from": "+000000000000",
                    "to": sent.to,
                    "status": "delivered",
                    "original_media_url": sent.media_url,
                    "created_at": sent.timestamp.isoformat(),
                    # No page count: nothing here opened the PDF, and inventing
                    # one would put a fabricated number on the fax report.
                }
        return None
