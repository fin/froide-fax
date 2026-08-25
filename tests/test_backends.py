"""FAX_BACKEND: the console/dummy transports.

The point of these is to exercise everything up to the wire -- fax message
creation, PDF rendering, attachment storage, delivery status, the polling sweep
-- without a Telnyx call. `no_telnyx_network` in conftest turns any real call
into an AssertionError, so an end-to-end test that passes here has provably not
touched the network.
"""

import io

from django.test import override_settings

import pytest

from froide_fax.backends import DEFAULT_FAX_BACKEND, get_fax_backend, outbox
from froide_fax.backends.console import ConsoleFaxBackend
from froide_fax.backends.dummy import DummyFaxBackend
from froide_fax.backends.telnyx import TelnyxFaxBackend

DUMMY = "froide_fax.backends.dummy.DummyFaxBackend"
CONSOLE = "froide_fax.backends.console.ConsoleFaxBackend"


@pytest.fixture(autouse=True)
def clear_outbox():
    outbox.clear()
    yield
    outbox.clear()


def test_telnyx_is_the_default():
    """Unset FAX_BACKEND must keep sending real faxes, not swallow them."""
    assert DEFAULT_FAX_BACKEND.endswith("TelnyxFaxBackend")
    assert isinstance(get_fax_backend(), TelnyxFaxBackend)


@override_settings(FAX_BACKEND=DUMMY)
def test_setting_selects_the_backend():
    assert isinstance(get_fax_backend(), DummyFaxBackend)


@override_settings(FAX_BACKEND=CONSOLE)
def test_backend_is_not_cached_across_override():
    """override_settings has to take effect, as it does for EMAIL_BACKEND."""
    assert isinstance(get_fax_backend(), ConsoleFaxBackend)


def test_dummy_records_and_accepts():
    result = DummyFaxBackend().send("+493012345678", "https://example.org/f.pdf")
    assert result.accepted
    assert result.fax_id
    assert len(outbox) == 1
    assert outbox[0].to == "+493012345678"


def test_dummy_reports_delivered_so_the_sweep_can_resolve():
    """Without this a fax would sit in STATUS_SENDING for ever: no webhook
    is ever going to arrive for a fax that was never sent."""
    result = DummyFaxBackend().send("+493012345678", "https://example.org/f.pdf")
    status = DummyFaxBackend().get_status(result.fax_id)
    assert status["status"] == "delivered"
    assert status["to"] == "+493012345678"


def test_status_of_an_unknown_fax_is_none():
    assert DummyFaxBackend().get_status("nope") is None


def test_dummy_status_has_no_invented_page_count():
    """Nothing here opened the PDF; a made-up number would reach the report."""
    result = DummyFaxBackend().send("+493012345678", "https://example.org/f.pdf")
    assert "page_count" not in DummyFaxBackend().get_status(result.fax_id)


def test_console_prints_the_fax():
    backend = ConsoleFaxBackend()
    backend.stream = io.StringIO()
    backend.send("+493012345678", "https://example.org/f.pdf")
    written = backend.stream.getvalue()
    assert "+493012345678" in written
    assert "https://example.org/f.pdf" in written
    assert "not sent" in written


@pytest.mark.django_db(transaction=True)
@override_settings(FAX_BACKEND=DUMMY)
def test_end_to_end_creates_the_pdf_without_sending(email_message):
    """The whole reason this exists: a real fax.pdf, and no network.

    transaction=True because create_fax_message() enqueues the send through
    transaction.on_commit, which never fires inside the usual test transaction.
    With CELERY_TASK_ALWAYS_EAGER the task then runs inline, so this covers the
    real path: message -> attachment -> PDF -> handler -> backend.

    conftest's no_telnyx_network turns any live Telnyx call into an
    AssertionError, so reaching the assertions at all proves nothing was sent.
    """
    from froide_fax.fax import FAX_ATTACHMENT_NAME, get_fax_attachment
    from froide_fax.models import Signature
    from froide_fax.utils import create_fax_message

    Signature.objects.create(user=email_message.request.user)
    # get_signature() memoises "no signature" and checks with hasattr, so the
    # attribute has to be removed rather than set to None.
    email_message.request.user.__dict__.pop("_signature", None)

    fax_message = create_fax_message(email_message, ignore_time=True)
    assert fax_message is not None

    att = get_fax_attachment(fax_message)
    assert att is not None, "no fax.pdf was rendered"
    assert att.name == FAX_ATTACHMENT_NAME
    assert att.filetype == "application/pdf"
    assert att.size > 0
    with att.file.open("rb") as fh:
        assert fh.read(4) == b"%PDF"

    # ...and it is no more public than a really-sent one.
    assert att.approved is False
    assert att.can_approve is False

    assert len(outbox) == 1
    fax_message.refresh_from_db()
    assert fax_message.email_message_id == outbox[0].fax_id
    assert fax_message.sent is True
