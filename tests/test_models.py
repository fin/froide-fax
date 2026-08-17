import base64

from django.core.files.base import ContentFile

import pytest

from froide.account.factories import UserFactory

from froide_fax.models import DATA_URL_PNG, Signature
from froide_fax.utils import get_signature

pytestmark = pytest.mark.django_db

# Smallest valid PNG: 1x1 transparent pixel.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_signature_dataurl_roundtrip(db):
    signature = Signature.objects.create(user=UserFactory())
    signature.signature.save("sig.png", ContentFile(PNG_BYTES))

    dataurl = signature.get_signature_dataurl()
    assert dataurl.startswith(DATA_URL_PNG)
    assert base64.b64decode(dataurl[len(DATA_URL_PNG) :]) == PNG_BYTES


def test_signature_without_image_has_no_dataurl(db):
    signature = Signature.objects.create(user=UserFactory())
    assert signature.get_signature_dataurl() is None
    assert signature.get_signature_bytes() is None


def test_remove_signature_file_clears_storage(db):
    signature = Signature.objects.create(user=UserFactory())
    signature.signature.save("sig.png", ContentFile(PNG_BYTES))

    signature.remove_signature_file()
    assert not signature.signature


def test_get_signature_caches_on_user(db):
    user = UserFactory()
    assert get_signature(user) is None
    # Miss is cached too, so a second call must not hit the database again.
    assert user._signature is None
    assert get_signature(None) is None
