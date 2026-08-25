import json
import re
from datetime import datetime, timedelta
from datetime import timezone as tz
from functools import partial
from typing import List, Optional

import phonenumbers
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.core.signing import BadSignature, Signer
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from froide.foirequest.models import DeliveryStatus, FoiMessage, FoiRequest
from froide.foirequest.models.message import MessageKind

from .models import Signature


def get_fax_region() -> str:
    """Default region for parsing national-format fax numbers.

    Was hardcoded to "DE", which silently rejects national-format numbers in
    every other jurisdiction running froide. Falls back to LANGUAGE_CODE, the
    same source froide.publicbody.validators.validate_fax uses.
    """
    region = getattr(settings, "FAX_NUMBER_REGION", None)
    if region:
        return region
    return settings.LANGUAGE_CODE.upper().split("-")[0]


def parse_fax_number(number: str, region: str = None) -> Optional[str]:
    """Return `number` in E164, or None if it is not a usable fax number.

    Pure: no database writes, no side effects.
    """
    if not number:
        return None
    if region is None:
        region = get_fax_region()
    try:
        parsed = phonenumbers.parse(number, region)
    except phonenumbers.phonenumberutil.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_publicbody_fax(publicbody) -> Optional[str]:
    """Rewrite publicbody.fax to E164 in place, clearing impossible numbers.

    Split out of ensure_fax_number so callers can opt out of writing to a row
    they do not own -- see ensure_fax_number(normalize=...).
    """
    if not publicbody.fax:
        return None

    try:
        parsed = phonenumbers.parse(publicbody.fax, get_fax_region())
    except phonenumbers.phonenumberutil.NumberParseException:
        return None

    if not phonenumbers.is_possible_number(parsed):
        # Not merely invalid: cannot be a phone number at all. Drop it so it
        # stops being offered as a fax destination.
        publicbody.fax = ""
        publicbody.save(update_fields=["fax"])
        return None

    if not phonenumbers.is_valid_number(parsed):
        return None

    fax_number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    if fax_number != publicbody.fax:
        publicbody.fax = fax_number
        publicbody.save(update_fields=["fax"])
    return fax_number


def ensure_fax_number(publicbody, normalize: bool = True) -> Optional[str]:
    """Return the public body's fax number in E164, or None.

    With normalize=True (the default, and the historical behaviour) the number
    is written back to the PublicBody row. Pass normalize=False from read-only
    paths -- notably anything running inside request creation, where writing to
    a PublicBody the requester does not own is a surprising side effect.
    """
    if publicbody is None:
        return None
    if normalize:
        return normalize_publicbody_fax(publicbody)
    return parse_fax_number(publicbody.fax)


def get_signature(user):
    if user is None:
        return None
    if hasattr(user, "_signature"):
        return user._signature
    try:
        signature = Signature.objects.get(user=user)
    except Signature.DoesNotExist:
        user._signature = None
        return None
    user._signature = signature
    return signature


FAX_MEDIA_SALT = "fax_media_url"
FAX_CALLBACK_SALT = "fax_callback_url"


def get_media_url(att):
    return get_signed_media_url(att)


def get_signed_media_url(att):
    attachment_signature = sign_obj_id(att.pk, salt=FAX_MEDIA_SALT)
    return settings.SITE_URL + reverse(
        "froide_fax-media_url", kwargs={"signed": attachment_signature}
    )


def get_status_callback_url(message):
    attachment_signature = sign_obj_id(message.pk, salt=FAX_CALLBACK_SALT)
    return settings.SITE_URL + reverse(
        "froide_fax-status_callback", kwargs={"signed": attachment_signature}
    )


def sign_obj_id(obj_id, salt=None):
    signer = Signer(salt=salt)
    value = signer.sign("%s@%s" % (obj_id, settings.TELNYX_APP_ID))
    return value


def unsign_attachment_id(signature):
    return unsign_obj_id(signature, salt=FAX_MEDIA_SALT)


def unsign_message_id(signature):
    return unsign_obj_id(signature, salt=FAX_CALLBACK_SALT)


def unsign_obj_id(signature, salt=None):
    signer = Signer(salt=salt)
    try:
        original = signer.unsign(signature)
    except BadSignature:
        return None
    parts = original.split("@", 1)
    if len(parts) != 2:
        return None
    if parts[1] != settings.TELNYX_APP_ID:
        return None
    return int(parts[0])


def can_send_fax_on_request(foirequest: FoiRequest, always_allow=False):
    if not foirequest.public_body:
        return False
    fax_number = ensure_fax_number(foirequest.public_body)
    if fax_number is None:
        return False
    if always_allow:
        return True

    return is_faxing_enabled_on_request(foirequest)


def is_faxing_enabled_on_request(foirequest: FoiRequest):
    return (
        foirequest.law and foirequest.law.requires_signature
    ) or foirequest.has_been_refused()


def message_can_be_faxed(
    message: FoiMessage,
    ignore_time: bool = False,
    ignore_signature: bool = False,
    ignore_law: bool = False,
) -> bool:
    if message is None:
        return False
    if not message.is_email:
        return False
    if message.is_response:
        return False

    foirequest = message.request
    if not ignore_law and not is_faxing_enabled_on_request(foirequest):
        return False

    if not message.recipient_public_body:
        return False

    fax_number = ensure_fax_number(message.recipient_public_body)
    if fax_number is None:
        return False

    sig = get_signature(foirequest.user)
    if not ignore_signature and not sig:
        return False

    not_too_long_ago = timezone.now() - timedelta(hours=36)
    if not ignore_time and message.timestamp < not_too_long_ago:
        return False

    already_faxed = set(
        [
            m.original_id
            for m in foirequest.messages
            if not m.is_response and m.kind == MessageKind.FAX
        ]
    )
    if message.id in already_faxed:
        return False

    return True


def get_faxable_messages_from_foirequest(
    foirequest: FoiRequest, **kwargs
) -> List[FoiMessage]:
    return [m for m in foirequest.messages if message_can_be_faxed(m, **kwargs)]


def send_messages_of_request(foirequest: FoiRequest) -> None:
    if not foirequest.law.requires_signature:
        return

    messages = get_faxable_messages_from_foirequest(foirequest)
    for message in messages:
        create_fax_message(message)


def create_fax_message(
    message: FoiMessage, ignore_time: bool = False, ignore_law: bool = False
) -> FoiMessage:
    from .tasks import send_fax_message_task

    if not message_can_be_faxed(
        message, ignore_time=ignore_time, ignore_law=ignore_law
    ):
        return

    fax_message = FoiMessage.objects.create(
        kind=MessageKind.FAX,
        request=message.request,
        subject=message.subject,
        subject_redacted=message.subject_redacted,
        is_response=False,
        sender_user=message.sender_user,
        sender_name=message.sender_name,
        sender_email=message.sender_email,
        recipient_email=message.recipient_public_body.fax,
        recipient_public_body=message.recipient_public_body,
        recipient=message.recipient,
        timestamp=timezone.now(),
        plaintext="",
        original=message,
    )
    transaction.on_commit(partial(send_fax_message_task.delay, fax_message.pk))
    return fax_message


def message_can_get_fax_report(message: FoiMessage) -> bool:
    if message.kind != MessageKind.FAX:
        return False

    try:
        deliverystatus = message.deliverystatus
    except DeliveryStatus.DoesNotExist:
        return False

    return deliverystatus.status in (
        DeliveryStatus.Delivery.STATUS_SENT,
        DeliveryStatus.Delivery.STATUS_RECEIVED,
    )


# The shape `parse_fax_log` returns and `froide_fax/report.html` renders. It is
# Twilio's field naming, kept deliberately: logs written before the Telnyx
# migration are still in the database and still have to render.
FAX_LOG_FIELDS = (
    "from_",
    "to",
    "sid",
    "status",
    "num_pages",
    "duration",
    "failure_reason",
    "date_created",
)


def _fax_log(*, from_, to, sid, status, num_pages, duration, failure_reason,
             date_created, **extra):
    """Assemble a log entry in the canonical shape.

    Every caller goes through here so that a status resolved by the webhook and
    the same status resolved by the polling sweep produce the same fields. They
    did not: the sweep stored Telnyx's REST object verbatim, whose keys are
    `from`, `id`, `page_count` and `call_duration_secs`, so the report template
    -- which reads `from_`, `sid`, `num_pages`, `duration` -- rendered those
    cells empty. Django resolves a missing key to '', so the report simply came
    out blank rather than erroring.
    """
    log = {
        "from_": from_,
        "to": to,
        "sid": sid,
        "status": status,
        "num_pages": num_pages or 0,
        "duration": duration or 0,
        "failure_reason": failure_reason,
        "date_created": date_created,
    }
    log.update(extra)
    return log


def fax_log_from_webhook(payload, occurred_at, attempt=None):
    """Canonical log from a status callback body (`data.payload`)."""
    return _fax_log(
        from_=payload.get("from"),
        to=payload.get("to"),
        sid=payload.get("fax_id"),
        status=payload.get("status"),
        num_pages=payload.get("page_count"),
        duration=payload.get("call_duration_secs"),
        failure_reason=payload.get("failure_reason"),
        date_created=occurred_at,
        webhook_attempt=attempt,
    )


def fax_log_from_api(fax_data):
    """Canonical log from a REST fax object (`GET /v2/faxes/{id}`).

    The REST object and the webhook payload disagree on two names: the fax id
    is `id` here and `fax_id` there, and the timestamp is `created_at` here and
    `occurred_at` there. Both spellings are accepted, because a caller handing
    this a webhook payload should get a correct entry rather than a silently
    empty one.

    Unmapped keys are carried through so nothing Telnyx sent is lost -- the
    sweep used to store the whole object, and dropping fields would trade one
    debugging problem for another.
    """
    mapped = {"from", "id", "fax_id", "page_count", "call_duration_secs",
              "created_at", "occurred_at", "to", "status", "failure_reason"}
    return _fax_log(
        from_=fax_data.get("from"),
        to=fax_data.get("to"),
        sid=fax_data.get("id") or fax_data.get("fax_id"),
        status=fax_data.get("status"),
        num_pages=fax_data.get("page_count"),
        duration=fax_data.get("call_duration_secs"),
        failure_reason=fax_data.get("failure_reason"),
        date_created=fax_data.get("created_at") or fax_data.get("occurred_at"),
        **{k: v for k, v in fax_data.items() if k not in mapped},
    )


def create_fax_log(previous_log, data):
    return json.dumps(data, cls=DjangoJSONEncoder)


def parse_fax_log(deliverystatus):
    log = deliverystatus.log
    try:
        data = json.loads(log)
        date_fields = ("date_created", "date_updated")
        for key in date_fields:
            try:
                data[key] = datetime.fromisoformat(data[key].replace("Z", ""))
                # Make datetimes timezone aware and set to UTC (as stored in DB)
                data[key] = data[key].replace(tzinfo=tz.utc)
            except (ValueError, KeyError):
                data[key] = None
        return data
    except ValueError:
        pass
    if "FaxSid: " in log:
        data = parse_twilio_fax_log(log)
        if data is None:
            return
        json_data = json.dumps(data, cls=DjangoJSONEncoder)
        deliverystatus.log = json_data
        deliverystatus.save(update_fields=["log"])
        return data


def parse_twilio_fax_log(log):
    sid_re = re.compile(r"FaxSid: (FX\w+)")
    csid_re = re.compile(r"RemoteStationId: ?(.*)")
    bitrate_re = re.compile(r"BitRate: (\d+)")
    match = sid_re.search(log)
    if match is None:
        return
    csid = csid_re.search(log).group(1)
    bit_rate = bitrate_re.search(log)
    if bit_rate:
        bit_rate = bit_rate.group(1)
    fax_sid = match.group(1)
    try:
        date_created = datetime.fromisoformat(log.splitlines()[0])
    except ValueError:
        date_created = None
    fax_data = {
        "num_pages": re.search(r"NumPages: (.*)", log).group(1),
        "from_": re.search(r"From: (.*)", log).group(1),
        "to": re.search(r"To: (.*)", log).group(1),
        "sid": fax_sid,
        "date_created": date_created,
    }
    fax_data["csid"] = csid
    fax_data["bit_rate"] = bit_rate
    fields = (
        "from_",
        "to",
        "quality",
        "num_pages",
        "duration",
        "status",
        "date_created",
        "date_updated",
        "sid",
        "csid",
        "bit_rate",
    )
    return {k: v for k, v in fax_data.items() if k in fields}


def message_can_be_resend(message):
    if message.kind != MessageKind.FAX:
        return False

    try:
        deliverystatus = message.deliverystatus
    except DeliveryStatus.DoesNotExist:
        return False

    return deliverystatus.status == DeliveryStatus.Delivery.STATUS_FAILED
