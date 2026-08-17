"""HTTP-level tests for the Telnyx status callback.

The payload bodies below are Telnyx's own documented examples, trimmed only
where a field is irrelevant here. The status-handling logic is unit-tested in
test_status.py; what this file pins is the *view* -- signature verification and,
above all, the response codes, because the status code is the only thing Telnyx
reacts to. A 5xx makes it redeliver the same payload indefinitely.
"""

import json
from datetime import timedelta

from django.core import mail
from django.urls import reverse
from django.utils import timezone

import pytest
from nacl.encoding import Base64Encoder
from nacl.signing import SigningKey

from froide.foirequest.models import DeliveryStatus
from froide.foirequest.models.message import MessageKind
from froide.foirequest.tests import factories

pytestmark = pytest.mark.django_db

Delivery = DeliveryStatus.Delivery

FAX_ID = "c62be5bc-9b13-4b6c-abda-34dd8b541287"


@pytest.fixture
def signing_key(settings):
    """Telnyx signs with its private key; we hold only the public half."""
    key = SigningKey.generate()
    settings.TELNYX_PUBLIC_KEY = key.verify_key.encode(Base64Encoder).decode()
    return key


@pytest.fixture
def fax_message(faxable_publicbody):
    """A fax sent *instead of* an email, awaiting its delivery callback."""
    foirequest = factories.FoiRequestFactory(public_body=faxable_publicbody)
    message = factories.FoiMessageFactory(
        request=foirequest,
        kind=MessageKind.FAX,
        recipient_public_body=faxable_publicbody,
        sender_user=foirequest.user,
        is_response=False,
        sent=True,
        status=None,
        original=None,
        email_message_id=FAX_ID,
    )
    DeliveryStatus.objects.create(
        message=message,
        status=Delivery.STATUS_SENDING,
        last_update=timezone.now() - timedelta(hours=2),
    )
    mail.outbox.clear()
    return message


def fax_event(status, fax_id=FAX_ID, **payload_overrides):
    """A Telnyx fax webhook body, in the documented data/meta envelope."""
    payload = {
        "call_duration_secs": 79,
        "connection_id": "1232154810234",
        "direction": "outbound",
        "fax_id": fax_id,
        "from": "+19459457421",
        "original_media_url": "https://example.org/dummy.pdf",
        "page_count": 1,
        "status": status,
        "to": "+13129457420",
        "user_id": "bdaa1f9f-1018-4156-867d-6c4ac9f556eb",
    }
    payload.update(payload_overrides)
    return {
        "data": {
            "event_type": "fax.%s" % status,
            "id": "3320554f-6b74-4138-a74b-a1e2ec7eaf8b",
            "occurred_at": "2022-01-07T10:01:43.677850Z",
            "record_type": "event",
            "payload": payload,
        },
        "meta": {"attempt": 1, "delivered_to": "https://example.org/webhooks"},
    }


def unwrapped(body):
    """The same event with Telnyx's ``data``/``meta`` envelope stripped.

    Telnyx documents fax.delivered and fax.failed in this shape.
    """
    return body["data"]


def post_callback(client, signing_key, body, timestamp=None, signature=None):
    if not isinstance(body, bytes):
        body = json.dumps(body).encode()
    ts = str(int((timestamp or timezone.now()).timestamp()))
    if signature is None:
        signed = signing_key.sign(("%s|" % ts).encode() + body)
        signature = Base64Encoder.encode(signed.signature).decode()
    return client.post(
        reverse("froide_fax-status_callback"),
        data=body,
        content_type="application/json",
        headers={
            "telnyx-timestamp": ts,
            "telnyx-signature-ed25519": signature,
        },
    )


class TestSignature:
    def test_valid_signature_is_accepted(self, client, signing_key, fax_message):
        response = post_callback(client, signing_key, fax_event("delivered"))
        assert response.status_code == 200

    def test_wrong_key_is_rejected(self, client, signing_key, fax_message):
        other = SigningKey.generate()
        body = json.dumps(fax_event("delivered")).encode()
        ts = str(int(timezone.now().timestamp()))
        forged = Base64Encoder.encode(
            other.sign(("%s|" % ts).encode() + body).signature
        ).decode()
        response = post_callback(
            client, signing_key, body, signature=forged
        )
        assert response.status_code == 403

    def test_tampered_body_is_rejected(self, client, signing_key, fax_message):
        body = json.dumps(fax_event("delivered")).encode()
        ts = str(int(timezone.now().timestamp()))
        signature = Base64Encoder.encode(
            signing_key.sign(("%s|" % ts).encode() + body).signature
        ).decode()
        tampered = body.replace(b"delivered", b"failed___")
        response = post_callback(client, signing_key, tampered, signature=signature)
        assert response.status_code == 403

    def test_get_is_not_allowed(self, client):
        response = client.get(reverse("froide_fax-status_callback"))
        assert response.status_code == 405


class TestTerminalStatuses:
    def test_delivered_marks_the_fax_sent(self, client, signing_key, fax_message):
        response = post_callback(client, signing_key, fax_event("delivered"))

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENT

    def test_delivered_confirms_to_the_requester(
        self, client, signing_key, fax_message
    ):
        post_callback(client, signing_key, fax_event("delivered"))

        # original is None, so no email told the requester anything yet.
        assert len(mail.outbox) == 1

    def test_failed_marks_the_fax_failed(
        self, client, signing_key, fax_message, monkeypatch
    ):
        monkeypatch.setattr(
            "froide_fax.tasks.retry_fax_delivery.apply_async",
            lambda *a, **kw: None,
        )
        response = post_callback(
            client,
            signing_key,
            fax_event("failed", failure_reason="user_busy"),
        )

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_FAILED

    def test_failure_reason_is_recorded(
        self, client, signing_key, fax_message, monkeypatch
    ):
        # The retry is scheduled eagerly and would overwrite the log.
        monkeypatch.setattr(
            "froide_fax.tasks.retry_fax_delivery.apply_async",
            lambda *a, **kw: None,
        )
        post_callback(
            client,
            signing_key,
            fax_event("failed", failure_reason="receiver_unallocated_number"),
        )

        fax_message.refresh_from_db()
        log = json.loads(fax_message.deliverystatus.log)
        assert log["failure_reason"] == "receiver_unallocated_number"


class TestAcknowledgedWithoutAction:
    """Everything Telnyx may send that we cannot act on must still get a 2xx."""

    @pytest.mark.parametrize("status", ["queued", "media.processed", "sending"])
    def test_in_progress_statuses(self, client, signing_key, fax_message, status):
        response = post_callback(client, signing_key, fax_event(status))

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING

    @pytest.mark.parametrize("status", ["receiving", "received"])
    def test_inbound_statuses_are_ignored(
        self, client, signing_key, fax_message, status
    ):
        response = post_callback(client, signing_key, fax_event(status))

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING

    def test_unknown_status_does_not_5xx(self, client, signing_key, fax_message):
        # A 5xx here would make Telnyx redeliver this payload forever.
        response = post_callback(client, signing_key, fax_event("teleported"))

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING

    def test_unknown_fax_id_does_not_5xx(self, client, signing_key, fax_message):
        response = post_callback(
            client, signing_key, fax_event("delivered", fax_id="not-our-fax")
        )

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING


class TestStaleCallbacks:
    def test_callback_older_than_our_state_is_refused(
        self, client, signing_key, fax_message
    ):
        ds = fax_message.deliverystatus
        ds.last_update = timezone.now()
        ds.save()

        response = post_callback(
            client,
            signing_key,
            fax_event("delivered"),
            # Inside the replay window, so the 409 is what is under test.
            timestamp=timezone.now() - timedelta(seconds=60),
        )

        assert response.status_code == 409
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING

    def test_first_callback_without_a_delivery_status_is_fine(
        self, client, signing_key, fax_message
    ):
        fax_message.deliverystatus.delete()

        response = post_callback(client, signing_key, fax_event("delivered"))

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENT


class TestEnvelope:
    """Telnyx's docs show both a wrapped and an unwrapped envelope.

    Accepting only one of them means every callback in the other shape 5xxs,
    and Telnyx redelivers it forever.
    """

    def test_unwrapped_delivered_is_accepted(
        self, client, signing_key, fax_message
    ):
        response = post_callback(
            client, signing_key, unwrapped(fax_event("delivered"))
        )

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENT

    def test_unwrapped_failed_is_accepted(
        self, client, signing_key, fax_message, monkeypatch
    ):
        monkeypatch.setattr(
            "froide_fax.tasks.retry_fax_delivery.apply_async",
            lambda *a, **kw: None,
        )
        response = post_callback(
            client,
            signing_key,
            unwrapped(fax_event("failed", failure_reason="user_busy")),
        )

        assert response.status_code == 200
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_FAILED

    def test_unwrapped_inbound_is_acknowledged(
        self, client, signing_key, fax_message
    ):
        response = post_callback(
            client, signing_key, unwrapped(fax_event("receiving"))
        )

        assert response.status_code == 200

    def test_both_envelopes_log_identically(
        self, client, signing_key, fax_message, faxable_publicbody
    ):
        post_callback(client, signing_key, fax_event("delivered"))
        fax_message.refresh_from_db()
        wrapped_log = json.loads(fax_message.deliverystatus.log)

        ds = fax_message.deliverystatus
        ds.status = Delivery.STATUS_SENDING
        ds.last_update = timezone.now() - timedelta(hours=2)
        ds.save()

        post_callback(client, signing_key, unwrapped(fax_event("delivered")))
        fax_message.refresh_from_db()
        unwrapped_log = json.loads(fax_message.deliverystatus.log)

        # meta.attempt only exists in the wrapped envelope; everything the fax
        # itself reports must be identical.
        assert unwrapped_log.pop("webhook_attempt") is None
        assert wrapped_log.pop("webhook_attempt") == 1
        assert unwrapped_log == wrapped_log


class TestMalformedHeaders:
    """This endpoint is public and csrf-exempt; bad input must not 5xx."""

    def test_missing_both_headers(self, client, fax_message):
        response = client.post(
            reverse("froide_fax-status_callback"),
            data=json.dumps(fax_event("delivered")),
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_missing_timestamp(self, client, signing_key, fax_message):
        response = client.post(
            reverse("froide_fax-status_callback"),
            data=json.dumps(fax_event("delivered")),
            content_type="application/json",
            headers={"telnyx-signature-ed25519": "irrelevant"},
        )
        assert response.status_code == 403

    def test_signature_that_is_not_base64(self, client, signing_key, fax_message):
        response = post_callback(
            client, signing_key, fax_event("delivered"), signature="!!!not base64!!!"
        )
        assert response.status_code == 403

    def test_timestamp_that_is_not_a_number(self, client, signing_key, fax_message):
        body = json.dumps(fax_event("delivered")).encode()
        response = client.post(
            reverse("froide_fax-status_callback"),
            data=body,
            content_type="application/json",
            headers={
                "telnyx-timestamp": "not-a-timestamp",
                "telnyx-signature-ed25519": "aaaa",
            },
        )
        assert response.status_code == 403


class TestReplayWindow:
    def test_old_but_validly_signed_payload_is_refused(
        self, client, signing_key, fax_message
    ):
        response = post_callback(
            client,
            signing_key,
            fax_event("delivered"),
            timestamp=timezone.now() - timedelta(hours=6),
        )

        assert response.status_code == 403
        fax_message.refresh_from_db()
        assert fax_message.deliverystatus.status == Delivery.STATUS_SENDING

    def test_far_future_timestamp_is_refused(self, client, signing_key, fax_message):
        response = post_callback(
            client,
            signing_key,
            fax_event("delivered"),
            timestamp=timezone.now() + timedelta(hours=6),
        )

        assert response.status_code == 403

    def test_just_inside_the_window_is_accepted(
        self, client, signing_key, fax_message
    ):
        response = post_callback(
            client,
            signing_key,
            fax_event("delivered"),
            timestamp=timezone.now() - timedelta(seconds=60),
        )

        assert response.status_code == 200


class TestRedeliveryVisibility:
    def test_attempt_is_recorded(self, client, signing_key, fax_message):
        body = fax_event("delivered")
        body["meta"]["attempt"] = 4

        post_callback(client, signing_key, body)

        fax_message.refresh_from_db()
        log = json.loads(fax_message.deliverystatus.log)
        assert log["webhook_attempt"] == 4

    def test_redelivery_is_logged(self, client, signing_key, fax_message, caplog):
        body = fax_event("delivered")
        body["meta"]["attempt"] = 3

        post_callback(client, signing_key, body)

        assert "redelivery attempt 3" in caplog.text

    def test_first_attempt_is_not_logged(
        self, client, signing_key, fax_message, caplog
    ):
        post_callback(client, signing_key, fax_event("delivered"))

        assert "redelivery attempt" not in caplog.text
