import pytest

from froide.publicbody.factories import PublicBodyFactory

from froide_fax.utils import (
    ensure_fax_number,
    get_fax_region,
    normalize_publicbody_fax,
    parse_fax_number,
)

pytestmark = pytest.mark.django_db


class TestRegion:
    def test_explicit_setting_wins(self, settings):
        settings.FAX_NUMBER_REGION = "AT"
        assert get_fax_region() == "AT"

    def test_falls_back_to_language_code(self, settings):
        settings.FAX_NUMBER_REGION = None
        settings.LANGUAGE_CODE = "de"
        assert get_fax_region() == "DE"

    def test_strips_language_subtag(self, settings):
        settings.FAX_NUMBER_REGION = None
        settings.LANGUAGE_CODE = "de-at"
        # "DE-AT" is not a region code; phonenumbers wants "DE".
        assert get_fax_region() == "DE"


class TestParseFaxNumber:
    def test_international_format_needs_no_region(self):
        assert parse_fax_number("+49 30 12345678") == "+493012345678"

    def test_national_format_uses_configured_region(self, settings):
        settings.FAX_NUMBER_REGION = "AT"
        assert parse_fax_number("01 5811234") == "+4315811234"

    def test_region_can_be_passed_explicitly(self):
        assert parse_fax_number("01 5811234", region="AT") == "+4315811234"

    def test_austrian_number_was_unusable_under_the_old_hardcoded_region(self):
        # Regression: parse() was called with a literal "DE", so a national
        # Austrian number never resolved and the body looked un-faxable.
        assert parse_fax_number("01 5811234", region="DE") != "+4315811234"

    @pytest.mark.parametrize("value", ["", None, "not a number", "1"])
    def test_unusable_values_return_none(self, value):
        assert parse_fax_number(value) is None

    def test_is_pure(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        parse_fax_number(publicbody.fax)
        publicbody.refresh_from_db()
        assert publicbody.fax == "+49 30 12345678"


class TestNormalize:
    def test_rewrites_to_e164(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        assert normalize_publicbody_fax(publicbody) == "+493012345678"
        publicbody.refresh_from_db()
        assert publicbody.fax == "+493012345678"

    def test_blanks_impossible_numbers(self, db):
        # A too-long number parses but is impossible; "1" does not parse at all,
        # so it never reaches the branch this test covers.
        publicbody = PublicBodyFactory(fax="+4930123456789012345")
        assert normalize_publicbody_fax(publicbody) is None
        publicbody.refresh_from_db()
        assert publicbody.fax == ""

    def test_keeps_merely_invalid_numbers(self, db):
        # Possible but not a valid allocation: don't destroy the record.
        publicbody = PublicBodyFactory(fax="+49 30 99999999999")
        normalize_publicbody_fax(publicbody)
        publicbody.refresh_from_db()
        assert publicbody.fax != ""


class TestEnsureFaxNumber:
    def test_normalizes_by_default(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        assert ensure_fax_number(publicbody) == "+493012345678"
        publicbody.refresh_from_db()
        assert publicbody.fax == "+493012345678"

    def test_normalize_false_leaves_the_row_alone(self, db):
        publicbody = PublicBodyFactory(fax="+49 30 12345678")
        assert ensure_fax_number(publicbody, normalize=False) == "+493012345678"
        publicbody.refresh_from_db()
        assert publicbody.fax == "+49 30 12345678"

    def test_handles_missing_publicbody(self):
        assert ensure_fax_number(None) is None
