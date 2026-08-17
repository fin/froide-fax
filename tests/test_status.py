from datetime import timedelta

from django.core import mail
from django.utils import timezone

import pytest

from froide.foirequest.models import DeliveryStatus
from froide.foirequest.models.message import MessageKind
from froide.foirequest.tests import factories
from froide.problem.models import ProblemReport

from froide_fax.models import FaxOverride
from froide_fax.status import (
    INBOUND_STATUSES,
    TELNYX_STATUS_MAP,
    apply_fax_status,
    map_telnyx_status,
)
from froide_fax.tasks import poll_fax_status, sweep_pending_faxes

pytestmark = pytest.mark.django_db

Delivery = DeliveryStatus.Delivery


@pytest.fixture
def fax_override(faxable_publicbody):
    return FaxOverride.objects.create(publicbody=faxable_publicbody, enabled=True)


@pytest.fixture
def replacement_fax(fax_override):
    """A fax that went out *instead of* an email: original is None."""
    foirequest = factories.FoiRequestFactory(public_body=fax_override.publicbody)
    message = factories.FoiMessageFactory(
        request=foirequest,
        kind=MessageKind.FAX,
        recipient_public_body=fax_override.publicbody,
        sender_user=foirequest.user,
        is_response=False,
        sent=True,
        status=None,
        original=None,
        email_message_id="fax-abc-123",
    )
    DeliveryStatus.objects.create(
        message=message,
        status=Delivery.STATUS_SENDING,
        last_update=timezone.now() - timedelta(hours=2),
    )
    mail.outbox.clear()
    return message


class TestStatusMapping:
    @pytest.mark.parametrize(
        "raw", ["queued", "initiated", "originated", "media.processing",
                "media.processed", "sending"]
    )
    def test_in_progress_states_map_to_sending(self, raw):
        assert map_telnyx_status(raw) == Delivery.STATUS_SENDING

    def test_terminal_states(self):
        assert map_telnyx_status("delivered") == Delivery.STATUS_SENT
        assert map_telnyx_status("failed") == Delivery.STATUS_FAILED

    def test_sending_prefix_still_matches(self):
        # Historical behaviour: Telnyx once emitted "sending.started".
        assert map_telnyx_status("sending.started") == Delivery.STATUS_SENDING

    def test_unknown_and_empty_return_none(self):
        # None means "do not act", never an exception -- raising here became a
        # 500 and Telnyx redelivered the same payload forever.
        assert map_telnyx_status("something.new") is None
        assert map_telnyx_status("") is None
        assert map_telnyx_status(None) is None

    def test_inbound_statuses_are_not_mapped(self):
        for raw in INBOUND_STATUSES:
            assert raw not in TELNYX_STATUS_MAP


class TestApplyFaxStatus:
    def test_delivered_confirms_a_replacement_fax(self, replacement_fax):
        ds = apply_fax_status(replacement_fax, Delivery.STATUS_SENT)

        assert ds.status == Delivery.STATUS_SENT
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to[0] == replacement_fax.request.user.email

    def test_delivered_does_not_confirm_a_copy_of_an_email(
        self, replacement_fax, email_message
    ):
        replacement_fax.original = email_message
        replacement_fax.save()

        apply_fax_status(replacement_fax, Delivery.STATUS_SENT)
        # The email already produced a confirmation; don't send a second one.
        assert len(mail.outbox) == 0

    def test_failed_schedules_a_retry_below_the_limit(
        self, replacement_fax, monkeypatch
    ):
        scheduled = []
        monkeypatch.setattr(
            "froide_fax.tasks.retry_fax_delivery.apply_async",
            lambda *a, **kw: scheduled.append((a, kw)),
        )
        apply_fax_status(replacement_fax, Delivery.STATUS_FAILED, log="busy")

        assert len(scheduled) == 1
        assert not ProblemReport.objects.filter(message=replacement_fax).exists()

    def test_failed_reports_a_problem_at_the_retry_limit(self, replacement_fax):
        ds = replacement_fax.deliverystatus
        ds.retry_count = 3
        ds.save()

        apply_fax_status(replacement_fax, Delivery.STATUS_FAILED, log="no answer")
        assert ProblemReport.objects.filter(
            message=replacement_fax, kind=ProblemReport.PROBLEM.BOUNCE_PUBLICBODY
        ).exists()


class TestSweep:
    def test_picks_up_stale_sending_faxes(self, replacement_fax, monkeypatch):
        queued = []
        monkeypatch.setattr(
            "froide_fax.tasks.poll_fax_status.delay", lambda pk: queued.append(pk)
        )
        assert sweep_pending_faxes() == 1
        assert queued == [replacement_fax.pk]

    def test_ignores_recently_updated_faxes(self, replacement_fax, monkeypatch):
        ds = replacement_fax.deliverystatus
        ds.last_update = timezone.now()
        ds.save()
        monkeypatch.setattr("froide_fax.tasks.poll_fax_status.delay", lambda pk: None)
        assert sweep_pending_faxes() == 0

    def test_ignores_resolved_faxes(self, replacement_fax, monkeypatch):
        ds = replacement_fax.deliverystatus
        ds.status = Delivery.STATUS_SENT
        ds.save()
        monkeypatch.setattr("froide_fax.tasks.poll_fax_status.delay", lambda pk: None)
        assert sweep_pending_faxes() == 0

    def test_ignores_faxes_without_a_provider_id(self, replacement_fax, monkeypatch):
        replacement_fax.email_message_id = ""
        replacement_fax.save()
        monkeypatch.setattr("froide_fax.tasks.poll_fax_status.delay", lambda pk: None)
        assert sweep_pending_faxes() == 0


class TestPoll:
    def test_delivered_result_is_applied(self, replacement_fax, monkeypatch):
        monkeypatch.setattr(
            "froide_fax.fax.get_fax",
            lambda fax_id: {"id": fax_id, "status": "delivered"},
        )
        assert poll_fax_status(replacement_fax.pk) == Delivery.STATUS_SENT

        replacement_fax.refresh_from_db()
        assert replacement_fax.deliverystatus.status == Delivery.STATUS_SENT
        assert len(mail.outbox) == 1

    def test_still_sending_does_not_reset_the_staleness_clock(
        self, replacement_fax, monkeypatch
    ):
        before = replacement_fax.deliverystatus.last_update
        monkeypatch.setattr(
            "froide_fax.fax.get_fax",
            lambda fax_id: {"id": fax_id, "status": "sending"},
        )
        poll_fax_status(replacement_fax.pk)

        replacement_fax.deliverystatus.refresh_from_db()
        assert replacement_fax.deliverystatus.last_update == before

    def test_unknown_fax_at_provider_is_survivable(self, replacement_fax, monkeypatch):
        monkeypatch.setattr("froide_fax.fax.get_fax", lambda fax_id: None)
        assert poll_fax_status(replacement_fax.pk) is None

    def test_missing_message_is_survivable(self):
        assert poll_fax_status(0) is None
