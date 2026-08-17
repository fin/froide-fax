from datetime import timedelta

from django.utils import timezone

import pytest

from froide.foirequest.models.message import MessageKind
from froide.foirequest.tests import factories
from froide.publicbody.factories import PublicBodyFactory

from froide_fax.models import Signature
from froide_fax.utils import (
    create_fax_message,
    ensure_fax_number,
    get_signature,
    is_faxing_enabled_on_request,
    message_can_be_faxed,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_user(faxable_request):
    Signature.objects.create(user=faxable_request.user)
    # get_signature() caches "no signature" as None and checks the cache with
    # hasattr, so the attribute has to be removed, not set to None.
    faxable_request.user.__dict__.pop("_signature", None)
    return faxable_request.user


class TestEnsureFaxNumber:
    def test_rewrites_to_e164_and_persists(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        assert ensure_fax_number(publicbody) == "+493012345678"
        publicbody.refresh_from_db()
        assert publicbody.fax == "+493012345678"

    def test_blanks_impossible_numbers(self, db):
        publicbody = PublicBodyFactory(fax="+4930123456789012345")
        assert ensure_fax_number(publicbody) is None
        publicbody.refresh_from_db()
        assert publicbody.fax == ""

    def test_returns_none_without_a_number(self, db):
        assert ensure_fax_number(PublicBodyFactory(fax="")) is None


class TestFaxingEnabled:
    def test_enabled_when_law_requires_signature(self, faxable_request):
        assert is_faxing_enabled_on_request(faxable_request)

    def test_disabled_for_ordinary_law(self, db):
        foirequest = factories.FoiRequestFactory()
        foirequest.law.requires_signature = False
        foirequest.law.save()
        assert not is_faxing_enabled_on_request(foirequest)


class TestMessageCanBeFaxed:
    def test_accepts_a_fresh_signed_outgoing_email(self, email_message, signed_user):
        assert message_can_be_faxed(email_message)

    def test_rejects_none(self):
        assert not message_can_be_faxed(None)

    def test_rejects_incoming_messages(self, email_message, signed_user):
        email_message.is_response = True
        assert not message_can_be_faxed(email_message)

    def test_rejects_non_email_kinds(self, email_message, signed_user):
        email_message.kind = MessageKind.POST
        assert not message_can_be_faxed(email_message)

    def test_rejects_without_a_signature(self, email_message):
        assert get_signature(email_message.request.user) is None
        assert not message_can_be_faxed(email_message)

    def test_signature_check_can_be_ignored(self, email_message):
        assert message_can_be_faxed(email_message, ignore_signature=True)

    def test_rejects_stale_messages(self, email_message, signed_user):
        email_message.timestamp = timezone.now() - timedelta(hours=48)
        assert not message_can_be_faxed(email_message)
        assert message_can_be_faxed(email_message, ignore_time=True)

    def test_rejects_without_recipient_publicbody(self, email_message, signed_user):
        email_message.recipient_public_body = None
        assert not message_can_be_faxed(email_message)

    def test_rejects_when_already_faxed(self, email_message, signed_user):
        create_fax_message(email_message)
        email_message.request._messages = None
        assert not message_can_be_faxed(email_message)


class TestCreateFaxMessage:
    def test_creates_a_copy_referencing_the_original(self, email_message, signed_user):
        fax_message = create_fax_message(email_message)

        assert fax_message is not None
        assert fax_message.kind == MessageKind.FAX
        assert fax_message.original_id == email_message.id
        assert fax_message.request_id == email_message.request_id
        assert not fax_message.is_response
        # The rendered letter is the payload; the copy carries no body text.
        assert fax_message.plaintext == ""

    def test_returns_none_when_not_faxable(self, email_message):
        # No signature stored for the user.
        assert create_fax_message(email_message) is None
