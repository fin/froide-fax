import logging

import requests
from django import forms
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from froide.foirequest.message_handlers import MessageHandler
from froide.foirequest.models import DeliveryStatus, FoiAttachment, FoiMessage
from froide.foirequest.models.message import MessageKind
from froide.helper.widgets import BootstrapCheckboxInput
from froide.problem.models import ProblemReport

from .backends import FaxSendResult, get_fax_backend
from .forms import SignatureField, save_signature_for_user
from .models import FaxOverride
from .pdf_generator import FaxMessagePDFGenerator
from .utils import (
    create_fax_log,
    create_fax_message,
    ensure_fax_number,
    format_fax_number,
    get_media_url,
    get_signature,
)

logger = logging.getLogger(__name__)

FAX_ATTACHMENT_NAME = "fax.pdf"


class FaxFailedException(Exception):
    msg: str

    def __init__(self, msg, *args, **kwargs):
        self.msg = msg
        super().__init__(*args, **kwargs)


def convert_to_fax_bytes(original_message: FoiMessage) -> bytes:
    pdf_generator = FaxMessagePDFGenerator(original_message)
    return pdf_generator.get_pdf_bytes()


def get_fax_source_message(fax_message: FoiMessage) -> FoiMessage:
    """The message whose content gets rendered onto the fax.

    Normally a fax is a copy of an email message and `original` points at it.
    When the fax *replaces* the email (FaxOverride) the message is the request
    itself and has no original.
    """
    return fax_message.original or fax_message


def get_fax_attachment(fax_message):
    for att in fax_message.attachments:
        if att.name == FAX_ATTACHMENT_NAME:
            return att
    return None


def send_email_copy(
    fax_message: FoiMessage, recipient_email: str, fax_number: str = ""
):
    """Send a plain-text email duplicate of a faxed message.

    For a FaxOverride that carries an ``email_copy`` address: the authority
    refuses email requests, but a caseworker or an archive still gets a
    readable copy. A real FoiMessage (``kind=EMAIL``, ``original`` pointing at
    the fax) so it shows in the request thread, and idempotent per fax message
    so a resend does not pile up copies. Returns the copy, or the existing one.

    ``fax_number`` is prepended as a one-line note so the recipient knows the
    request also went out by fax.
    """
    if not recipient_email:
        return None

    existing = FoiMessage.objects.filter(
        original=fax_message,
        kind=MessageKind.EMAIL,
        recipient_email=recipient_email,
    ).first()
    if existing is not None:
        return existing

    source = get_fax_source_message(fax_message)
    publicbody = fax_message.recipient_public_body

    def _with_note(body):
        if not fax_number:
            return body
        note = _("(Sent by email and by fax to %(number)s.)") % {
            "number": format_fax_number(fax_number)
        }
        return "%s\n\n%s" % (note, body)

    subject_prefix = _("Fax copy: ")

    copy = FoiMessage.objects.create(
        request=fax_message.request,
        kind=MessageKind.EMAIL,
        is_response=False,
        original=fax_message,
        subject=subject_prefix + fax_message.subject,
        subject_redacted=subject_prefix + fax_message.subject_redacted,
        sender_user=fax_message.sender_user,
        sender_name=fax_message.sender_name,
        sender_email=fax_message.sender_email,
        recipient_email=recipient_email,
        recipient_public_body=publicbody,
        recipient=(publicbody.name if publicbody else fax_message.recipient),
        plaintext=_with_note(source.plaintext),
        plaintext_redacted=_with_note(source.plaintext_redacted),
        timestamp=timezone.now(),
    )
    copy.send()
    return copy


def create_fax_attachment(fax_message):
    att = FoiAttachment(
        belongs_to=fax_message,
        name=FAX_ATTACHMENT_NAME,
        is_redacted=False,
        filetype="application/pdf",
        approved=False,
        can_approve=False,
    )
    pdf_bytes = convert_to_fax_bytes(get_fax_source_message(fax_message))
    pdf_file = ContentFile(pdf_bytes)
    att.size = pdf_file.size
    att.file.save(att.name, pdf_file)
    att.save()
    fax_message._attachments = None
    return att


def send_fax_message(fax_message):
    if not fax_message.kind == MessageKind.FAX:
        return

    create_fax_attachment(fax_message)

    fax_message.send(notify=False)
    return fax_message


def send_fax_telnyx(
    to,
    from_,
    media_url,
    connection_id,
    authorization="",
    quality="high",
):
    """this sends a single message through the telnyx fax gateway
    results / error to be handled by calling instance"""
    data = {
        "to": to,
        "from": from_,
        "media_url": media_url,
        "connection_id": connection_id,  # this is a misnomer, app_id goes here
        "quality": quality,  # choice of normal, high, very_high
    }

    headers = {
        "Authorization": authorization,
    }

    sent_at = timezone.now()
    response = requests.post(
        "https://api.telnyx.com/v2/faxes", headers=headers, data=data
    )

    # Always record the outcome of the send: without this, a fax that Telnyx
    # accepts with an unexpected body (wrong status code, missing data.id) is
    # invisible -- the id never gets stored and every later status webhook
    # silently fails to match a message.
    logger.info(
        "Telnyx fax send to %s at %s -> HTTP %s: %s",
        to,
        sent_at.isoformat(),
        response.status_code,
        response.text[:2000],
    )

    try:
        response.raise_for_status()
    except Exception:
        error_data = response.json()
        logger.error("Fax sending failed %s", error_data)
        raise FaxFailedException(response.text)
    return response


def get_fax_telnyx(fax_id, authorization=""):
    """Fetch one fax's current state.

    Telnyx's list endpoint cannot filter by status, so the sweep drives from
    our own DeliveryStatus rows and looks each fax up by id.
    """
    response = requests.get(
        "https://api.telnyx.com/v2/faxes/%s" % fax_id,
        headers={"Authorization": authorization},
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json().get("data")


def get_fax(fax_id):
    """Current state of one fax, via the configured backend."""
    return get_fax_backend().get_status(fax_id)


def send_fax(fax_number, media_url) -> FaxSendResult:
    """Hand one fax to the configured backend.

    `settings.FAX_BACKEND` selects it; the default is the real Telnyx
    transport, so behaviour is unchanged unless it is set. The console and
    dummy backends exercise message creation, PDF rendering and attachment
    storage without any network call.
    """
    return get_fax_backend().send(fax_number, media_url)


class FaxMessageHandler(MessageHandler):
    @classmethod
    def handle_foirequest_outgoing_messages(cls, foirequest, recipient_email=None):
        """Claim a request whose public body is marked fax-only.

        Called by froide's ``get_request_outgoing_message_kind()`` when it
        decides an outgoing message's kind (froide commit 7520e6a2b). The method
        name must match froide's base ``MessageHandler`` hook exactly -- the
        earlier name ``handle_request_outgoing_messages`` was silently never
        invoked. On a froide without that mechanism this is simply never called
        and the package behaves exactly as before.

        ``recipient_email`` is set when a reply picks a specific address. Divert
        to fax only when that address is the public body's own default -- the
        one that refuses email. A different address (a mediator, an alternative
        responsibility address, or one the authority itself replied from) is
        left to go out by email. ``None`` -- the initial request, where the
        recipient is the body's default anyway -- keeps diverting.
        """
        publicbody = getattr(foirequest, "public_body", None)
        if not FaxOverride.objects.is_fax_recipient(publicbody):
            return False
        if recipient_email is None:
            return True
        default = (publicbody.email or "").strip().lower()
        return bool(default) and recipient_email.strip().lower() == default

    def get_fax_number(self):
        """Resolve the number for this message's actual recipient.

        Prefers an explicit FaxOverride so a fax-only body is dialled on the
        configured line, and falls back to the public body's own number for the
        original mode. normalize=False: this can run inside request creation,
        where writing to a PublicBody row would be a surprising side effect.
        """
        message = self.message
        publicbody = message.recipient_public_body
        override = FaxOverride.objects.get_for_publicbody(publicbody)
        if override is not None:
            return override.number
        return ensure_fax_number(publicbody, normalize=False)

    def run_send(self, **kwargs):
        fax_message = self.message

        fax_number = self.get_fax_number()
        if fax_number is None:
            return None

        # When the fax replaces the email, nothing has rendered the document
        # yet -- send_fax_message() only runs for the copy-of-an-email flow.
        att = get_fax_attachment(fax_message)
        if att is None:
            att = create_fax_attachment(fax_message)

        media_url = get_media_url(att)

        ds, created = DeliveryStatus.objects.update_or_create(
            message=fax_message,
            defaults=dict(
                status=DeliveryStatus.Delivery.STATUS_SENDING,
                last_update=timezone.now(),
            ),
        )
        try:
            result = send_fax(fax_number, media_url)
        except FaxFailedException as e:
            ds.status = DeliveryStatus.Delivery.STATUS_FAILED
            ds.log = create_fax_log(
                ds.log,
                {
                    "status": "failed",
                    "detail": e.msg,
                    "date_created": timezone.now(),
                },
            )
            ds.save()
            # The provider rejected the send outright: the fax never entered the
            # queue, so no status webhook will ever arrive to trigger the
            # ProblemReport that a delivery-time failure does. Raise one here so
            # the failure reaches the moderation queue instead of sitting in a
            # DeliveryStatus row nobody looks at. No auto-retry -- an API
            # rejection means a malformed request or a misconfigured account,
            # and resending the same payload fails identically.
            ProblemReport.objects.report(
                message=fax_message,
                kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY,
                description=ds.log,
                auto_submitted=True,
            )
            return

        # Store the Telnyx fax id in 'email_message_id' (a misnomer) -- the
        # status webhook looks the message up by it.
        #
        # Set it on the instance *and* persist, matching EmailMessageHandler.
        # froide's request-creation flow does `message.save()` immediately after
        # `message.send()`; a bare `.filter(...).update(...)` here would be
        # overwritten by that save writing back the stale in-memory instance,
        # leaving email_message_id empty and every webhook unmatched.
        fax_message.email_message_id = result.fax_id
        fax_message.sent = result.accepted
        fax_message.save(update_fields=["email_message_id", "sent"])

        override = FaxOverride.objects.get_for_publicbody(
            fax_message.recipient_public_body
        )
        if override is not None and override.email_copy:
            # The fax already went; a failed copy must not fail the send.
            try:
                send_email_copy(
                    fax_message, override.email_copy, fax_number=override.number
                )
            except Exception:
                logger.exception(
                    "Fax email copy to %s failed for message %s",
                    override.email_copy,
                    fax_message.pk,
                )

    @classmethod
    def _get_metadata(cls, form):
        foirequest = getattr(form, "foirequest", None)
        if foirequest:
            return foirequest, [foirequest]
        return form.foiproject, form.foirequests

    @classmethod
    def initialize_send_message_form(cls, form):
        meta_obj, foirequests = cls._get_metadata(form)
        if not any(ensure_fax_number(fr.public_body) for fr in foirequests):
            return
        form.fields["send_fax"] = forms.BooleanField(
            required=False,
            label=_("Send message as fax"),
            help_text=_(
                "In addition to email you can send this message as a fax if the response needs to be signed."
            ),
            widget=BootstrapCheckboxInput,
        )
        additional_render_fields = [form["send_fax"]]
        signature = get_signature(meta_obj.user)
        # Only if no signature is present, we show the field
        if not signature:
            form.fields["signature"] = SignatureField(required=False)
            additional_render_fields.append(form["signature"])
        form.additional_render_fields = additional_render_fields

    @classmethod
    def clean_send_message_form(cls, form, cleaned_data):
        meta_obj, foirequests = cls._get_metadata(form)
        if not any(ensure_fax_number(fr.public_body) for fr in foirequests):
            return

        if not cleaned_data["send_fax"]:
            return cleaned_data

        signature = get_signature(meta_obj.user)
        if not signature and not cleaned_data["signature"]:
            form.add_error(
                "signature",
                _("You need to provide a signature to send a fax message."),
            )
        return cleaned_data

    @classmethod
    def save_send_message_form(cls, form, message, user):
        if not ensure_fax_number(message.request.public_body):
            return

        if message.request.user != user:
            # Can't set signature for different user!
            return
        if form.cleaned_data["send_fax"]:
            if "signature" in form.cleaned_data:
                save_signature_for_user(user, form.cleaned_data["signature"])
            create_fax_message(message)
