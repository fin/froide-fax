import logging

import requests
from django import forms
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from froide.foirequest.message_handlers import MessageHandler
from froide.foirequest.models import DeliveryStatus, FoiAttachment, FoiMessage
from froide.foirequest.models.message import MessageKind
from froide.helper.widgets import BootstrapCheckboxInput

from .forms import SignatureField, save_signature_for_user
from .models import FaxOverride
from .pdf_generator import FaxMessagePDFGenerator
from .utils import create_fax_message, ensure_fax_number, get_media_url, get_signature

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

    response = requests.post(
        "https://api.telnyx.com/v2/faxes", headers=headers, data=data
    )

    try:
        response.raise_for_status()
    except Exception:
        error_data = response.json()
        logger.error("Fax sending failed %s", error_data)
        raise FaxFailedException(response.text)
    return response


def send_fax(fax_number, media_url):
    return send_fax_telnyx(
        to=fax_number,
        from_=settings.TELNYX_FROM_NUMBER,
        media_url=media_url,
        connection_id=settings.TELNYX_APP_ID,
        authorization=f"Bearer {settings.TELNYX_API_KEY}",
    )


class FaxMessageHandler(MessageHandler):
    @classmethod
    def handle_request_outgoing_messages(cls, foirequest):
        """Claim a request whose public body is marked fax-only.

        Called by froide when it decides an outgoing message's kind. On a froide
        without that mechanism this is simply never invoked and the package
        behaves exactly as before.
        """
        return FaxOverride.objects.is_fax_recipient(
            getattr(foirequest, "public_body", None)
        )

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
            fax_response = send_fax(fax_number, media_url)
        except FaxFailedException as e:
            ds.status = DeliveryStatus.Delivery.STATUS_FAILED
            ds.log = e.msg
            ds.save()
            return

        fax_data = fax_response.json().get("data")
        if fax_data:
            fax_id = fax_data.get("id", "")

        sent = fax_response.status_code == 202
        # store fax.sid in message 'email_message_id' (misnomer)
        FoiMessage.objects.filter(pk=fax_message.pk).update(
            email_message_id=fax_id, sent=sent
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
