"""send_fax_telnyx's contract with its callers, with only the HTTP call faked.

conftest's no_telnyx_network replaces send_fax_telnyx wholesale, so no other
test runs the real function. That hid a break when merging okfde/main:
upstream changed it to return the bare fax id, while TelnyxFaxBackend.send()
and send_test_fax() read .json() / .status_code off the Response. These
tests put the real function back and fake only requests.post.
"""

import json

import pytest
import requests

from froide_fax.backends.telnyx import TelnyxFaxBackend
from froide_fax.fax import FaxFailedException
from froide_fax.fax import send_fax_telnyx as real_send_fax_telnyx


def make_response(status_code, body):
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode() if isinstance(body, str) else body
    response.url = "https://api.telnyx.com/v2/faxes"
    response.reason = "test"
    return response


@pytest.fixture
def telnyx_post(monkeypatch):
    monkeypatch.setattr("froide_fax.fax.send_fax_telnyx", real_send_fax_telnyx)
    calls = []

    def install(response):
        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return response

        monkeypatch.setattr("froide_fax.fax.requests.post", fake_post)
        return calls

    return install


def test_backend_reads_fax_id_from_accepted_response(telnyx_post):
    calls = telnyx_post(
        make_response(202, json.dumps({"data": {"id": "fax-123", "status": "queued"}}))
    )

    result = TelnyxFaxBackend().send("+4311234567", "https://example.org/fax.pdf")

    assert result.fax_id == "fax-123"
    assert result.accepted is True
    assert len(calls) == 1
    assert calls[0][1]["data"]["to"] == "+4311234567"


def test_non_202_success_is_not_accepted(telnyx_post):
    telnyx_post(make_response(200, json.dumps({"data": {"id": "fax-123"}})))

    result = TelnyxFaxBackend().send("+4311234567", "https://example.org/fax.pdf")

    assert result.fax_id == "fax-123"
    assert result.accepted is False


def test_http_error_with_non_json_body_raises_fax_failed(telnyx_post):
    # The pre-merge code called response.json() on the error path, which itself
    # raised on an HTML error page and masked the real failure.
    telnyx_post(make_response(502, "<html>Bad Gateway</html>"))

    with pytest.raises(FaxFailedException) as excinfo:
        real_send_fax_telnyx(
            to="+4311234567",
            from_="+4330000000",
            media_url="https://example.org/fax.pdf",
            connection_id="app",
        )

    assert "Bad Gateway" in excinfo.value.msg
    assert isinstance(excinfo.value.__cause__, requests.HTTPError)
