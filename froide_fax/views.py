import datetime
import json
import logging
from urllib.parse import urljoin

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import FormView

import pytz
import requests
from nacl.encoding import Base64Encoder
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from froide.foirequest.auth import can_write_foirequest
from froide.foirequest.models import DeliveryStatus, FoiAttachment, FoiMessage
from froide.helper.utils import get_redirect_url

from froide_fax.fax import convert_to_fax_bytes

from .forms import SignatureForm
from .models import FAX_PERMISSION
from .pdf_generator import FaxReportPDFGenerator
from .tasks import retry_fax_delivery
from .status import (
    INBOUND_STATUSES,
    apply_fax_status,
    map_telnyx_status,
    unwrap_event,
)
from .utils import (
    create_fax_log,
    create_fax_message,
    fax_log_from_webhook,
    message_can_be_faxed,
    message_can_be_resend,
    message_can_get_fax_report,
    unsign_attachment_id,
)

logger = logging.getLogger(__name__)


def fax_media_url(request, signed):
    attachment_id = unsign_attachment_id(signed)
    if attachment_id is None:
        return HttpResponse(status=403)

    attachment = get_object_or_404(FoiAttachment, pk=attachment_id)
    url = attachment.get_absolute_domain_file_url(authorized=True)

    # get_absolute_domain_file_url() only prepends the *domain part* of
    # MEDIA_URL, so it is relative whenever MEDIA_URL has no host -- the default
    # in development ("/files/"). requests.get() then raises MissingSchema.
    # Resolve against SITE_URL; a URL that is already absolute is left as is.
    url = urljoin(settings.SITE_URL, url)

    # Telnyx does not support redirects
    # So stream response from CDN URL here
    response = requests.get(url, stream=True)
    return StreamingHttpResponse(
        response.raw,
        content_type=response.headers.get("content-type"),
        status=response.status_code,
        reason=response.reason,
    )


# Telnyx documents a five-minute tolerance on the signature timestamp. Without
# it a validly signed payload stays replayable for as long as the signing key
# lives.
WEBHOOK_TOLERANCE_SECONDS = 5 * 60


def _forbidden(reason):
    return HttpResponseForbidden(reason, content_type="text/plain")


@csrf_exempt
@require_POST
def fax_status_callback(request: HttpRequest):
    # Log the raw body once, before any parsing, so a rejected or mis-shaped
    # webhook can be diagnosed from the delivery Telnyx says it made. DEBUG so
    # it stays out of production logs.
    logger.debug("Telnyx fax webhook body: %s", request.body[:2000])

    # get relevant signature data
    event_timestamp = request.headers.get("Telnyx-Timestamp")
    event_signature = request.headers.get("Telnyx-Signature-Ed25519")
    public_key = settings.TELNYX_PUBLIC_KEY

    # Absent or unparsable headers are a rejected request, not a server error.
    # This endpoint is public and csrf-exempt, so anyone can post to it.
    if not event_timestamp or not event_signature:
        return _forbidden("missing signature headers")

    try:
        signature = Base64Encoder.decode(event_signature)
    except Exception:
        return _forbidden("malformed signature")

    try:
        sent_at = datetime.datetime.fromtimestamp(
            int(event_timestamp), pytz.timezone("UTC")
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return _forbidden("malformed timestamp")

    # prepare signature data for nacl
    verify_key = VerifyKey(public_key, encoder=Base64Encoder)
    callback_bytes = f"{event_timestamp}|".encode("UTF-8") + request.body

    # verify signature
    try:
        verify_key.verify(callback_bytes, signature=signature)
    except BadSignatureError:
        return _forbidden("invalid signature")

    if abs((timezone.now() - sent_at).total_seconds()) > WEBHOOK_TOLERANCE_SECONDS:
        return _forbidden("stale timestamp")

    payload_json = json.loads(request.body)
    data = unwrap_event(payload_json)

    # get message object
    fax_id = None
    try:
        fax_id = data.get("payload").get("fax_id")
    except AttributeError as e:
        # this key should always exist. we should never end up here
        raise ValueError(
            f"This is not a valid API response body: {request.body}"
        ) from e

    if not fax_id:
        raise ValueError(f"This is not a valid API response body: {request.body}")

    raw_status = data.get("payload", {}).get("status")
    if raw_status in INBOUND_STATUSES:
        # Inbound faxes arrive on the same application webhook. We do not
        # receive faxes; acknowledge so Telnyx stops redelivering.
        return HttpResponse(status=200)

    status = map_telnyx_status(raw_status)
    if status is None:
        # Well-formed but unrecognised. Returning 5xx here would make Telnyx
        # redeliver the same payload indefinitely without ever succeeding.
        logger.warning("Unhandled Telnyx fax status %r for fax %s", raw_status, fax_id)
        return HttpResponse(status=200)

    try:
        fax_message: FoiMessage = FoiMessage.objects.get(email_message_id=fax_id)
    except FoiMessage.DoesNotExist:
        logger.warning("Telnyx callback for unknown fax id %s", fax_id)
        return HttpResponse(status=200)

    # only try and update if the timestamp in request is more recent than
    # the one in the database
    try:
        if fax_message.deliverystatus.last_update > sent_at:
            return HttpResponse(status=409)
    except DeliveryStatus.DoesNotExist:
        pass

    # Telnyx counts its own redelivery attempts. Anything above 1 means an
    # earlier attempt did not get a 2xx out of us.
    attempt = payload_json.get("meta", {}).get("attempt")
    if attempt and attempt > 1:
        logger.warning(
            "Telnyx redelivery attempt %s for fax %s (status %r)",
            attempt,
            fax_id,
            raw_status,
        )

    # Create machine-readable log
    fax_log_data = fax_log_from_webhook(
        data["payload"], data["occurred_at"], attempt=attempt
    )

    apply_fax_status(
        fax_message,
        status,
        log=create_fax_log(None, fax_log_data),
        failure_reason=fax_log_data["failure_reason"],
    )

    return HttpResponse(status=200)


class UpdateSignatureView(LoginRequiredMixin, FormView):
    form_class = SignatureForm
    template_name = "froide_fax/form.html"

    def get_form_kwargs(self):
        kwargs = super(UpdateSignatureView, self).get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def form_valid(self, form):
        """If the form is valid, redirect to the supplied URL."""
        sig = form.save()
        if sig:
            messages.add_message(
                self.request, messages.SUCCESS, _("Signature has been saved.")
            )
        else:
            messages.add_message(
                self.request, messages.SUCCESS, _("Signature has been removed.")
            )
        return super(UpdateSignatureView, self).form_valid(form)

    def get_success_url(self):
        return get_redirect_url(self.request)


@require_POST
def send_as_fax(request, message_id):
    message = get_object_or_404(FoiMessage, id=message_id)
    if not can_write_foirequest(message.request, request):
        return HttpResponse(status=403)

    ignore_law = request.user.has_perm(FAX_PERMISSION)
    if not message_can_be_faxed(message, ignore_time=True, ignore_law=ignore_law):
        return HttpResponse(status=400)

    fax_message = create_fax_message(message, ignore_time=True, ignore_law=ignore_law)

    return redirect(fax_message)


@require_POST
def resend_fax(request, message_id):
    message = get_object_or_404(FoiMessage, id=message_id)

    if not can_write_foirequest(message.request, request):
        return HttpResponse(status=403)

    if not message_can_be_resend(message):
        return HttpResponse(status=400)

    retry_fax_delivery.delay(message.pk)

    return redirect(message)


def preview_fax(request, message_id):
    message = get_object_or_404(FoiMessage, id=message_id)
    if not can_write_foirequest(message.request, request):
        return HttpResponse(status=403)

    ignore_law = request.user.has_perm(FAX_PERMISSION)
    if not message_can_be_faxed(message, ignore_time=True, ignore_law=ignore_law):
        return HttpResponse(status=400)

    return HttpResponse(convert_to_fax_bytes(message), content_type="application/pdf")


def pdf_report(request, message_id):
    message = get_object_or_404(FoiMessage, id=message_id)
    if not can_write_foirequest(message.request, request):
        return HttpResponse(status=403)

    if not message_can_get_fax_report(message):
        return HttpResponse(status=404)

    pdf_generator = FaxReportPDFGenerator(message)

    response = HttpResponse(
        pdf_generator.get_pdf_bytes(), content_type="application/pdf"
    )
    response["Content-Disposition"] = (
        "attachment; " 'filename="fax-report-%s.pdf"' % message.pk
    )
    return response
