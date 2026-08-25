"""Prints faxes instead of sending them. Django's console email backend."""

import sys

from .base import RecordingFaxBackend


class ConsoleFaxBackend(RecordingFaxBackend):
    stream = None

    def record(self, sent):
        stream = self.stream or sys.stdout
        stream.write(
            "\n".join(
                [
                    "-" * 70,
                    f"Fax {sent.fax_id} to {sent.to}",
                    f"  PDF: {sent.media_url}",
                    "  (not sent: FAX_BACKEND is the console backend)",
                    "-" * 70,
                    "",
                ]
            )
        )
        stream.flush()
