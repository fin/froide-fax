"""Mapping and application of Telnyx fax statuses.

Extracted from the webhook view so the webhook and the polling sweep apply
statuses identically. The webhook remains the primary signal; polling is a
backstop for callbacks that never arrive.
"""

import logging
from typing import Optional

from django.utils import timezone

from froide.foirequest.models import DeliveryStatus, FoiMessage
from froide.problem.models import ProblemReport

logger = logging.getLogger(__name__)

Delivery = DeliveryStatus.Delivery

# Full outbound status set from the Telnyx OpenAPI spec. The previous mapping
# omitted initiated/originated/media.processing, and an unmapped value raised,
# which turned into a 500 and made Telnyx redeliver the same payload forever.
TELNYX_STATUS_MAP = {
    "queued": Delivery.STATUS_SENDING,
    "initiated": Delivery.STATUS_SENDING,
    "originated": Delivery.STATUS_SENDING,
    "media.processing": Delivery.STATUS_SENDING,
    "media.processed": Delivery.STATUS_SENDING,
    "sending": Delivery.STATUS_SENDING,
    "delivered": Delivery.STATUS_SENT,
    "failed": Delivery.STATUS_FAILED,
}

# Inbound-only statuses. This package sends faxes and never receives them, but
# both directions arrive on the same application webhook, so they are known-and-
# ignored rather than unknown.
INBOUND_STATUSES = frozenset({"receiving", "received"})

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 15 * 60


def map_telnyx_status(raw_status: Optional[str]) -> Optional[str]:
    """Translate a Telnyx status into a froide DeliveryStatus, or None."""
    if not raw_status:
        return None
    if raw_status in TELNYX_STATUS_MAP:
        return TELNYX_STATUS_MAP[raw_status]
    if raw_status.startswith("sending"):
        # Historical prefix match, kept in case Telnyx sub-types this state.
        return Delivery.STATUS_SENDING
    return None


def apply_fax_status(
    fax_message: FoiMessage,
    delivery_status: str,
    log: str = "",
    schedule_retry: bool = True,
) -> DeliveryStatus:
    """Record a resolved status and run the follow-on effects.

    Shared by the webhook and the polling sweep so both produce identical
    state, notifications and retries.
    """
    from .delivery import send_fax_sent_confirmation
    from .tasks import retry_fax_delivery

    ds, _created = DeliveryStatus.objects.update_or_create(
        message=fax_message,
        defaults=dict(status=delivery_status, last_update=timezone.now()),
    )
    if log:
        ds.log = log
        ds.save(update_fields=["log"])

    if delivery_status == Delivery.STATUS_SENT:
        fax_message.timestamp = ds.last_update
        fax_message.save()
        ProblemReport.objects.find_and_resolve(
            message=fax_message, kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY
        )
        if fax_message.original_id is None:
            # This fax replaced the email rather than accompanying one, so
            # nothing has confirmed to the requester yet.
            send_fax_sent_confirmation(fax_message)
        return ds

    if delivery_status == Delivery.STATUS_FAILED:
        if ds.retry_count >= MAX_RETRIES:
            ProblemReport.objects.report(
                message=fax_message,
                kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY,
                description=ds.log,
                auto_submitted=True,
            )
        elif schedule_retry:
            retry_fax_delivery.apply_async(
                (fax_message.pk,),
                {},
                # 0.25, 1, 4 hours
                countdown=RETRY_BASE_SECONDS * 4**ds.retry_count,
            )

    return ds
