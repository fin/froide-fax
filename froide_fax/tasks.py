import json
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone, translation

from froide.celery import app as celery_app
from froide.foirequest.models import DeliveryStatus, FoiMessage
from froide.foirequest.models.message import MessageKind

from .utils import create_fax_message

logger = logging.getLogger(__name__)

# Telnyx callbacks normally land within seconds. Anything still SENDING after
# this long suggests the webhook never arrived. Independent of how often the
# sweep runs: this defines "stuck", the beat interval decides how quickly a
# stuck fax is noticed.
STALE_AFTER = timedelta(minutes=30)
SWEEP_LIMIT = 200


@celery_app.task
def send_message_as_fax_task(message_id):
    translation.activate(settings.LANGUAGE_CODE)

    try:
        message = FoiMessage.objects.get(pk=message_id)
    except FoiMessage.DoesNotExist:
        return

    create_fax_message(message)


@celery_app.task
def send_fax_message_task(message_id):
    from .fax import send_fax_message

    translation.activate(settings.LANGUAGE_CODE)

    try:
        message = FoiMessage.objects.get(pk=message_id)
    except FoiMessage.DoesNotExist:
        return

    send_fax_message(message)


@celery_app.task
def retry_fax_delivery(message_id):
    translation.activate(settings.LANGUAGE_CODE)

    try:
        message = FoiMessage.objects.get(pk=message_id)
    except FoiMessage.DoesNotExist:
        return

    message.resend()


@celery_app.task
def send_test_fax():
    from .fax import send_fax_telnyx

    """
    send test faxes regularly, possibly with a distinct APP_ID, to gather receipts and ensure fax sending works as intended
    """
    to = settings.faxtest_receive_number
    from_ = settings.TELNYX_FROM_NUMBER
    media_url = settings.faxtest_pdf_url
    connection_id = settings.faxtest_app_id or settings.TELNYX_APP_ID
    authorization = f"Bearer {settings.TELNYX_API_KEY}"

    api_answer = send_fax_telnyx(
        to=to,
        from_=from_,
        media_url=media_url,
        connection_id=connection_id,
        authorization=authorization,
    )

    assert api_answer.status_code == 202
    # further process results here


@celery_app.task
def sweep_pending_faxes(stale_minutes=None, limit=SWEEP_LIMIT):
    """Find faxes stuck in SENDING and re-check them against the Telnyx API.

    The webhook is the primary signal; this is a backstop for callbacks that
    were never delivered (misconfigured application webhook, an outage, or a
    payload we rejected). Without it a fax that replaces an email can leave the
    requester with no confirmation at all and no sign anything went wrong.

    Schedule from CELERY_BEAT_SCHEDULE, e.g. every four hours. This is a
    backstop for a broken webhook, not a substitute for one.
    """
    if stale_minutes is None:
        stale = STALE_AFTER
    else:
        stale = timedelta(minutes=stale_minutes)

    cutoff = timezone.now() - stale
    stuck = (
        DeliveryStatus.objects.filter(
            message__kind=MessageKind.FAX,
            status=DeliveryStatus.Delivery.STATUS_SENDING,
            last_update__lt=cutoff,
        )
        .exclude(message__email_message_id="")
        .values_list("message_id", flat=True)[:limit]
    )

    message_ids = list(stuck)
    if message_ids:
        logger.info("Polling %s fax(es) with no delivery callback", len(message_ids))
    for message_id in message_ids:
        poll_fax_status.delay(message_id)
    return len(message_ids)


@celery_app.task
def poll_fax_status(message_id):
    """Ask Telnyx for one fax's status and apply it as a callback would."""
    from .fax import get_fax
    from .status import INBOUND_STATUSES, apply_fax_status, map_telnyx_status

    translation.activate(settings.LANGUAGE_CODE)

    try:
        message = FoiMessage.objects.get(pk=message_id)
    except FoiMessage.DoesNotExist:
        return

    fax_id = message.email_message_id
    if not fax_id:
        return

    fax_data = get_fax(fax_id)
    if fax_data is None:
        logger.warning("Telnyx has no record of fax %s (message %s)", fax_id, message_id)
        return

    raw_status = fax_data.get("status")
    if raw_status in INBOUND_STATUSES:
        return

    status = map_telnyx_status(raw_status)
    if status is None:
        logger.warning("Unhandled Telnyx fax status %r for fax %s", raw_status, fax_id)
        return

    if status == DeliveryStatus.Delivery.STATUS_SENDING:
        # Still in flight; nothing to record, and rewriting last_update would
        # reset the staleness clock and hide a genuinely stuck fax.
        return

    apply_fax_status(message, status, log=json.dumps(fax_data, default=str))
    return status
