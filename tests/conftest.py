import pytest
from django.utils import timezone

from froide.foirequest.tests import factories
from froide.publicbody.factories import (
    FoiLawFactory,
    JurisdictionFactory,
    PublicBodyFactory,
)

FAX_NUMBER = "+493012345678"


@pytest.fixture
def signature_law(db):
    """A law that requires a signature, which is what enables faxing."""
    return FoiLawFactory(
        jurisdiction=JurisdictionFactory(), requires_signature=True
    )


@pytest.fixture
def faxable_publicbody(db):
    return PublicBodyFactory(fax=FAX_NUMBER)


@pytest.fixture
def faxable_request(signature_law, faxable_publicbody):
    return factories.FoiRequestFactory(
        law=signature_law, public_body=faxable_publicbody
    )


@pytest.fixture
def email_message(faxable_request, faxable_publicbody):
    """A fresh outgoing email message, the input to create_fax_message()."""
    return factories.FoiMessageFactory(
        request=faxable_request,
        recipient_public_body=faxable_publicbody,
        sender_user=faxable_request.user,
        is_response=False,
        sent=True,
        status=None,
        # The factory default is a fixed date in the past, which the 36h
        # freshness check in message_can_be_faxed() always rejects.
        timestamp=timezone.now(),
    )
