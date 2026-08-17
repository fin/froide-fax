"""User notification for faxes that replace an email.

In this package's original mode a fax accompanies an email, and the email has
already produced froide's "Your Freedom of Information Request was sent"
confirmation -- so the fax needs no notification of its own.

When the fax *replaces* the email (see FaxOverride) there is no such mail, and
the requester would otherwise be told nothing at all.

froide's own send_foimessage_sent_confirmation cannot be reused: it returns
early on message.is_not_email. The function below is that function with the
guard removed. It should be deleted once froide grows a transport-neutral
delivery hook.
"""

from django.utils.translation import gettext_lazy as _

from froide.foirequest.models import FoiMessage
from froide.foirequest.signals import (
    confirm_foi_message_sent_email,
    confirm_foi_request_sent_email,
)
from froide.foirequest.utils import send_request_user_email, short_request_url


def send_fax_sent_confirmation(message: FoiMessage) -> bool:
    """Tell the requester their faxed request went out. True if a mail was sent."""
    request = message.request

    if message.is_bulk:
        return False
    if message.confirmation_sent:
        return False

    messages = request.get_messages()
    start_thread = False
    if len(messages) >= 1 and message == messages[0]:
        if request.project_id is not None:
            return False
        subject = _("Your Freedom of Information Request was sent")
        mail_intent = confirm_foi_request_sent_email
        action_url = request.get_absolute_domain_short_url()
        start_thread = True
    else:
        subject = _("Your message was sent")
        mail_intent = confirm_foi_message_sent_email
        action_url = message.get_absolute_domain_short_url()

    upload_url = request.user.get_autologin_url(
        short_request_url("foirequest-upload_postal_message_create", request)
    )

    send_request_user_email(
        mail_intent,
        request,
        subject=subject,
        context={
            "foirequest": request,
            "user": request.user,
            "publicbody": message.recipient_public_body,
            "message": message,
            "action_url": action_url,
            "upload_action_url": upload_url,
        },
        start_thread=start_thread,
    )

    message.confirmation_sent = True
    message.save(update_fields=["confirmation_sent"])
    return True
