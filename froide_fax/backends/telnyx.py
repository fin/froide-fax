"""The real transport. Default, so behaviour is unchanged unless configured."""

from django.conf import settings

from .base import BaseFaxBackend, FaxSendResult


class TelnyxFaxBackend(BaseFaxBackend):
    def send(self, to, media_url):
        # Imported here, not at module level: the test suite blocks live calls
        # by monkeypatching these names on froide_fax.fax, which only works if
        # they are looked up at call time. It also avoids a circular import.
        from froide_fax.fax import send_fax_telnyx

        response = send_fax_telnyx(
            to=to,
            from_=settings.TELNYX_FROM_NUMBER,
            media_url=media_url,
            connection_id=settings.TELNYX_APP_ID,
            authorization=f"Bearer {settings.TELNYX_API_KEY}",
        )
        data = response.json().get("data") or {}
        return FaxSendResult(
            fax_id=data.get("id", ""),
            accepted=response.status_code == 202,
        )

    def get_status(self, fax_id):
        from froide_fax.fax import get_fax_telnyx

        return get_fax_telnyx(fax_id, authorization=f"Bearer {settings.TELNYX_API_KEY}")
