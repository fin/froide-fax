import pytest

from froide.foirequest.message_handlers import (
    get_message_handler_class,
    get_request_outgoing_message_kind,
)
from froide.foirequest.models.message import MessageKind
from froide.foirequest.tests import factories
from froide.publicbody.factories import PublicBodyFactory

from froide_fax.fax import FaxMessageHandler, get_fax_source_message
from froide_fax.models import FaxOverride
from froide_fax.utils import message_can_be_faxed

pytestmark = pytest.mark.django_db


@pytest.fixture
def fax_override(faxable_publicbody):
    return FaxOverride.objects.create(publicbody=faxable_publicbody, enabled=True)


class TestFaxOverrideModel:
    def test_number_falls_back_to_publicbody_fax(self, fax_override):
        assert fax_override.number == "+493012345678"
        assert fax_override.is_usable

    def test_own_number_wins(self, faxable_publicbody):
        override = FaxOverride.objects.create(
            publicbody=faxable_publicbody, fax_number="+43 1 5811234"
        )
        assert override.number == "+4315811234"

    def test_disabled_is_not_usable(self, fax_override):
        fax_override.enabled = False
        assert not fax_override.is_usable
        fax_override.save()
        assert not FaxOverride.objects.is_fax_recipient(fax_override.publicbody)

    def test_enabled_without_a_number_is_not_usable(self, db):
        publicbody = PublicBodyFactory(fax="")
        override = FaxOverride.objects.create(publicbody=publicbody, enabled=True)
        # Must fall back to email rather than divert to a number we don't have.
        assert not override.is_usable
        assert not FaxOverride.objects.is_fax_recipient(publicbody)

    def test_lookup_of_unknown_and_none(self, db, fax_override):
        assert FaxOverride.objects.is_fax_recipient(fax_override.publicbody)
        assert not FaxOverride.objects.is_fax_recipient(PublicBodyFactory())
        assert not FaxOverride.objects.is_fax_recipient(None)


class TestRouting:
    def test_handler_is_registered_for_fax_kind(self):
        assert get_message_handler_class(MessageKind.FAX) is FaxMessageHandler

    def test_claims_request_to_overridden_body(self, fax_override):
        foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
        assert FaxMessageHandler.handle_foirequest_outgoing_messages(foirequest)

    def test_froide_routes_overridden_request_to_fax_kind(self, fax_override):
        """The hook must be wired to froide's dispatcher, not just callable.

        froide calls ``handle_foirequest_outgoing_messages`` by that exact name;
        a mismatch leaves this returning None and the request goes out by email.
        """
        foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
        assert get_request_outgoing_message_kind(foirequest) == MessageKind.FAX

    def test_froide_routes_normal_request_to_email_kind(self, db):
        foirequest = factories.FoiRequestFactory(public_body=PublicBodyFactory())
        assert get_request_outgoing_message_kind(foirequest) in (
            None,
            MessageKind.EMAIL,
        )

    def test_does_not_claim_other_bodies(self, db):
        foirequest = factories.FoiRequestFactory(public_body=PublicBodyFactory())
        assert not FaxMessageHandler.handle_foirequest_outgoing_messages(foirequest)

    def test_does_not_claim_request_without_body(self, db):
        # public_body is null=True / SET_NULL, so orphaned requests exist.
        foirequest = factories.FoiRequestFactory(public_body=None)
        assert not FaxMessageHandler.handle_foirequest_outgoing_messages(foirequest)


class TestSourceMessage:
    def test_copy_renders_its_original(self, email_message):
        fax_message = factories.FoiMessageFactory(
            request=email_message.request,
            kind=MessageKind.FAX,
            is_response=False,
            status=None,
            original=email_message,
        )
        assert get_fax_source_message(fax_message) == email_message

    def test_replacement_renders_itself(self, fax_override):
        foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
        message = factories.FoiMessageFactory(
            request=foirequest,
            kind=MessageKind.FAX,
            is_response=False,
            status=None,
            original=None,
        )
        assert get_fax_source_message(message) == message


class TestNoDoubleFaxing:
    def test_a_replacement_fax_is_not_faxed_again(self, fax_override):
        """The two modes must not both act on the same message.

        message_can_be_faxed() requires kind == EMAIL, so a message already
        marked FAX by the routing hook is skipped by the copy-of-an-email
        flow. That interlock is load-bearing; assert it.
        """
        foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
        message = factories.FoiMessageFactory(
            request=foirequest,
            kind=MessageKind.FAX,
            recipient_public_body=fax_override.publicbody,
            sender_user=foirequest.user,
            is_response=False,
            status=None,
        )
        assert not message_can_be_faxed(message, ignore_signature=True, ignore_law=True)


class TestNumberResolution:
    def test_override_number_preferred(self, fax_override):
        foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
        fax_override.fax_number = "+43 1 5811234"
        fax_override.save()
        message = factories.FoiMessageFactory(
            request=foirequest,
            kind=MessageKind.FAX,
            recipient_public_body=fax_override.publicbody,
            is_response=False,
            status=None,
        )
        assert FaxMessageHandler(message).get_fax_number() == "+4315811234"

    def test_falls_back_to_publicbody_number(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        foirequest = factories.FoiRequestFactory(public_body=publicbody)
        message = factories.FoiMessageFactory(
            request=foirequest,
            kind=MessageKind.FAX,
            recipient_public_body=publicbody,
            is_response=False,
            status=None,
        )
        assert FaxMessageHandler(message).get_fax_number() == "+493012345678"

    def test_resolution_does_not_write_to_publicbody(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        foirequest = factories.FoiRequestFactory(public_body=publicbody)
        message = factories.FoiMessageFactory(
            request=foirequest,
            kind=MessageKind.FAX,
            recipient_public_body=publicbody,
            is_response=False,
            status=None,
        )
        FaxMessageHandler(message).get_fax_number()
        publicbody.refresh_from_db()
        assert publicbody.fax == "+49 30 12345678"
