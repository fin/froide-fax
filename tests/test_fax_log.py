"""The DeliveryStatus log shape.

The webhook and the polling sweep both resolve a fax's final status, and
`apply_fax_status` is shared so that they produce the same state. The log was
the exception: the sweep stored Telnyx's REST object verbatim, whose keys are
`from`, `id`, `page_count` and `call_duration_secs`, while the report template
reads `from_`, `sid`, `num_pages` and `duration`.

Nothing raised. Django resolves a missing template key to '', so a fax whose
status happened to arrive by poll rather than by callback produced a report PDF
with blank sender, recipient, page count, duration and date -- and one that
arrived by callback did not. These tests pin the two paths together.
"""

import json

from froide_fax.utils import (
    FAX_LOG_FIELDS,
    create_fax_log,
    fax_log_from_api,
    fax_log_from_webhook,
)

# `GET /v2/faxes/{id}`
API_FAX = {
    "id": "fax-1234",
    "record_type": "fax",
    "direction": "outbound",
    "from": "+4930000000",
    "to": "+493012345678",
    "status": "delivered",
    "page_count": 3,
    "call_duration_secs": 42,
    "created_at": "2026-08-25T10:00:00.000Z",
    "connection_id": "conn-1",
    "original_media_url": "https://example.org/fax.pdf",
}

# `data.payload` of a status callback
WEBHOOK_PAYLOAD = {
    "fax_id": "fax-1234",
    "direction": "outbound",
    "from": "+4930000000",
    "to": "+493012345678",
    "status": "delivered",
    "page_count": 3,
    "call_duration_secs": 42,
    "connection_id": "conn-1",
}
OCCURRED_AT = "2026-08-25T10:00:00.000Z"


def test_both_paths_produce_the_same_canonical_fields():
    from_api = fax_log_from_api(API_FAX)
    from_hook = fax_log_from_webhook(WEBHOOK_PAYLOAD, OCCURRED_AT)
    for field in FAX_LOG_FIELDS:
        assert from_api[field] == from_hook[field], field


def test_api_object_is_mapped_off_telnyx_key_names():
    """The bug: these four keys are spelled differently in the REST object."""
    log = fax_log_from_api(API_FAX)
    assert log["from_"] == "+4930000000"
    assert log["sid"] == "fax-1234"
    assert log["num_pages"] == 3
    assert log["duration"] == 42


def test_report_template_fields_are_all_present():
    """froide_fax/report.html reads these; a missing key renders as ''."""
    for log in (
        fax_log_from_api(API_FAX),
        fax_log_from_webhook(WEBHOOK_PAYLOAD, OCCURRED_AT),
    ):
        for field in ("from_", "to", "num_pages", "duration", "date_created"):
            assert log.get(field) not in (None, ""), field


def test_api_path_keeps_unmapped_telnyx_fields():
    """The sweep used to store the whole object; do not lose it."""
    log = fax_log_from_api(API_FAX)
    assert log["connection_id"] == "conn-1"
    assert log["original_media_url"] == "https://example.org/fax.pdf"


def test_api_adapter_accepts_the_webhook_spellings_too():
    """Handed a payload by mistake, it should still fill sid and date."""
    log = fax_log_from_api({**WEBHOOK_PAYLOAD, "occurred_at": OCCURRED_AT})
    assert log["sid"] == "fax-1234"
    assert log["date_created"] == OCCURRED_AT


def test_missing_counters_become_zero_not_none():
    """report.html prints these directly; None would render as 'None'."""
    log = fax_log_from_api({"id": "x", "from": "a", "to": "b", "status": "failed"})
    assert log["num_pages"] == 0
    assert log["duration"] == 0


def test_webhook_attempt_is_recorded():
    log = fax_log_from_webhook(WEBHOOK_PAYLOAD, OCCURRED_AT, attempt=2)
    assert log["webhook_attempt"] == 2


def test_survives_the_json_round_trip():
    """create_fax_log is what actually lands in DeliveryStatus.log."""
    for log in (
        fax_log_from_api(API_FAX),
        fax_log_from_webhook(WEBHOOK_PAYLOAD, OCCURRED_AT),
    ):
        assert json.loads(create_fax_log(None, log))[-1]["sid"] == "fax-1234"


def test_create_fax_log_appends_oldest_first():
    first = create_fax_log(None, {"status": "queued"})
    second = create_fax_log(first, {"status": "media.processed"})
    third = create_fax_log(second, {"status": "delivered"})

    assert [e["status"] for e in json.loads(third)] == [
        "queued",
        "media.processed",
        "delivered",
    ]


def test_create_fax_log_keeps_a_non_array_previous_as_entry_zero():
    from_object = json.loads(create_fax_log('{"status": "old"}', {"status": "new"}))
    assert [e["status"] for e in from_object] == ["old", "new"]

    from_text = json.loads(create_fax_log("FaxSid: FX123\nTo: x", {"status": "new"}))
    assert from_text[0] == "FaxSid: FX123\nTo: x"
    assert from_text[1] == {"status": "new"}
