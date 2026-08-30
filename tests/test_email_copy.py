"""FaxOverride.email_copy: an optional email duplicate of every faxed message.

Some authorities refuse email requests (hence the fax) but a caseworker or an
archive address still wants a readable copy. When email_copy is set, run_send()
also creates a kind=EMAIL FoiMessage in the thread and sends it.
"""

from django.core import mail

import pytest

from froide.foirequest.models import FoiMessage
from froide.foirequest.models.message import MessageKind
from froide.foirequest.tests import factories

from froide_fax.fax import FaxMessageHandler, send_email_copy
from froide_fax.models import FaxOverride

pytestmark = pytest.mark.django_db

COPY_TO = "archive@authority.example.org"


@pytest.fixture(autouse=True)
def _backends(settings):
    settings.FAX_BACKEND = "froide_fax.backends.dummy.DummyFaxBackend"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def _fax_message(publicbody, body="Please send me the documents."):
    foirequest = factories.FoiRequestFactory(public_body=publicbody)
    return factories.FoiMessageFactory(
        request=foirequest,
        kind=MessageKind.FAX,
        recipient_public_body=publicbody,
        sender_user=foirequest.user,
        is_response=False,
        status=None,
        original=None,
        plaintext=body,
        plaintext_redacted=body,
    )


def test_copy_is_created_and_sent(faxable_publicbody):
    FaxOverride.objects.create(
        publicbody=faxable_publicbody, enabled=True, email_copy=COPY_TO
    )
    fax = _fax_message(faxable_publicbody)

    FaxMessageHandler(fax).run_send()

    copy = FoiMessage.objects.get(original=fax, kind=MessageKind.EMAIL)
    assert copy.request_id == fax.request_id
    assert copy.recipient_email == COPY_TO
    assert copy.is_response is False
    assert copy.sent is True
    assert copy.subject.startswith("Fax copy: ")
    assert copy.subject.endswith(fax.subject)
    # the fax-notice line, then the request body
    assert "by fax to +49 30 12345678" in copy.plaintext
    assert "Please send me the documents." in copy.plaintext
    assert [m for m in mail.outbox if m.to == [COPY_TO]]


def test_copy_carries_no_fax_note_when_no_number_is_given(faxable_publicbody):
    fax = _fax_message(faxable_publicbody)
    copy = send_email_copy(fax, COPY_TO)  # fax_number defaults to ""
    assert copy.plaintext == "Please send me the documents."


def test_no_copy_without_the_field(faxable_publicbody):
    FaxOverride.objects.create(publicbody=faxable_publicbody, enabled=True)
    fax = _fax_message(faxable_publicbody)

    FaxMessageHandler(fax).run_send()

    assert not FoiMessage.objects.filter(original=fax, kind=MessageKind.EMAIL).exists()
    assert mail.outbox == []


def test_copy_is_idempotent(faxable_publicbody):
    FaxOverride.objects.create(
        publicbody=faxable_publicbody, enabled=True, email_copy=COPY_TO
    )
    fax = _fax_message(faxable_publicbody)

    first = send_email_copy(fax, COPY_TO)
    again = send_email_copy(fax, COPY_TO)

    assert first.pk == again.pk
    assert FoiMessage.objects.filter(original=fax, kind=MessageKind.EMAIL).count() == 1


def test_send_failure_of_the_copy_does_not_break_the_fax(
    faxable_publicbody, monkeypatch
):
    FaxOverride.objects.create(
        publicbody=faxable_publicbody, enabled=True, email_copy=COPY_TO
    )
    fax = _fax_message(faxable_publicbody)

    def boom(self, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(
        "froide.foirequest.message_handlers.EmailMessageHandler.run_send", boom
    )

    FaxMessageHandler(fax).run_send()  # must not raise

    fax.refresh_from_db()
    assert fax.sent is True
