import datetime
import json

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
from froide.problem.models import ProblemReport

from froide_fax.fax import convert_to_fax_bytes

from .delivery import send_fax_sent_confirmation
from .forms import SignatureForm
from .models import FAX_PERMISSION
from .pdf_generator import FaxReportPDFGenerator
from .tasks import retry_fax_delivery
from .utils import (
    create_fax_log,
    create_fax_message,
    message_can_be_faxed,
    message_can_be_resend,
    message_can_get_fax_report,
    unsign_attachment_id,
)


def fax_media_url(request, signed):
    attachment_id = unsign_attachment_id(signed)
    if attachment_id is None:
        return HttpResponse(status=403)

    attachment = get_object_or_404(FoiAttachment, pk=attachment_id)
    url = attachment.get_absolute_domain_file_url(authorized=True)

    # Telnyx does not support redirects
    # So stream response from CDN URL here
    response = requests.get(url, stream=True)
    return StreamingHttpResponse(
        response.raw,
        content_type=response.headers.get("content-type"),
        status=response.status_code,
        reason=response.reason,
    )


@csrf_exempt
@require_POST
def fax_status_callback(request: HttpRequest):
    # get relevant signature data
    event_timestamp = request.headers.get("Telnyx-Timestamp")
    event_signature = request.headers.get("Telnyx-Signature-Ed25519")
    public_key = settings.TELNYX_PUBLIC_KEY

    # prepare signature data for nacl
    verify_key = VerifyKey(public_key, encoder=Base64Encoder)
    callback_bytes = f"{event_timestamp}|".encode("UTF-8") + request.body
    signature = Base64Encoder.decode(event_signature)

    # verify signature
    try:
        verify_key.verify(callback_bytes, signature=signature)
    except BadSignatureError:
        return HttpResponseForbidden("invalid signature", content_type="text/plain")

    payload_json = json.loads(request.body)

    # get message object
    fax_id = None
    try:
        fax_id = payload_json.get("data").get("payload").get("fax_id")
    except AttributeError as e:
        # this key should always exist. we should never end up here
        raise ValueError(
            f"This is not a valid API response body: {request.body}"
        ) from e

    if not fax_id:
        raise ValueError(f"This is not a valid API response body: {request.body}")

    fax_message: FoiMessage = get_object_or_404(FoiMessage, email_message_id=fax_id)

    # find status
    try:
        status = payload_json.get("data").get("payload").get("status")
    except AttributeError as e:
        # we should never end up here either
        raise ValueError(
            f"This is not a valid API response body: {request.body}"
        ) from e

    if status == "failed":
        status = DeliveryStatus.Delivery.STATUS_FAILED
    elif status == "queued":
        status = DeliveryStatus.Delivery.STATUS_SENDING
    elif status == "media.processed":
        status = DeliveryStatus.Delivery.STATUS_SENDING
    elif status.startswith("sending"):
        status = DeliveryStatus.Delivery.STATUS_SENDING
    elif status == "delivered":
        status = DeliveryStatus.Delivery.STATUS_SENT
    else:
        # again: we should not end up here. according to telnyx-docu those
        # are all possible stati
        raise ValueError(f"This is not a valid status response: {status}")

    # only try and update if the timestamp in request is more recent than
    # the one in the database
    dt = datetime.datetime.fromtimestamp(int(event_timestamp), pytz.timezone("UTC"))
    if fax_message.deliverystatus.last_update > dt:
        return HttpResponse(status=409)

    ds, _created = DeliveryStatus.objects.update_or_create(
        message=fax_message,
        defaults=dict(
            status=status,
            last_update=timezone.now(),
        ),
    )
    data = payload_json.get("data")

    # Create machine-readable log
    fax_log_data = {
        "from_": data["payload"]["from"],
        "to": data["payload"]["to"],
        "sid": data["payload"]["fax_id"],
        "status": data["payload"]["status"],
        "num_pages": data["payload"].get("page_count", 0),
        "duration": data["payload"].get("call_duration_secs", 0),
        "failure_reason": data["payload"].get("failure_reason"),
        "date_created": data["occurred_at"],
    }
    ds.log = create_fax_log(ds.log, fax_log_data)
    ds.save()

    if status == DeliveryStatus.Delivery.STATUS_SENT:
        fax_message.timestamp = ds.last_update
        fax_message.save()
        ProblemReport.objects.find_and_resolve(
            message=fax_message, kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY
        )
        if fax_message.original_id is None:
            # This fax replaced the email rather than accompanying one, so no
            # confirmation has reached the requester yet.
            send_fax_sent_confirmation(fax_message)

    failed = False
    if status == DeliveryStatus.Delivery.STATUS_FAILED:
        if ds.retry_count >= 3:
            failed = True
        else:
            # Retry fax delivery in 15 minutes
            retry_fax_delivery.apply_async(
                (fax_message.pk,),
                {},
                # resend in intervals of 0.25, 1, 2 and 4 hours
                countdown=15 * 60 * 4**ds.retry_count,
            )

    if failed:
        ProblemReport.objects.report(
            message=fax_message,
            kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY,
            description=ds.log,
            auto_submitted=True,
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
