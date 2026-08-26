# Froide Fax

A Django app that handles Fax sending for FragDenStaat.de.

This app works with froide and provides the following:

- a `froide_fax.fax.FaxMessageHandler` that can be configured to handle messages of type `fax`
- a model that stores a signature per user
- templates that can be included for getting a signature and sending a message as a fax.
- a `fax_tags` template tag library that provides:
  - a tag `get_signature_form` to render a form to get user's signature
  - a tag `foirequest_needs_signature` to check if an FOI request contains faxable messages and if the user should be asked to provide a signature
  - a tag `can_fax_message` that checks if a given message can be faxed
- URLs and views to:
  - store user signature
  - explicit trigger to fax a message
  - Webhook status callback of fax API provider
  - Authenticated view for PDF that should be faxed for API provider


## Delivery status

Telnyx reports fax progress to the webhook configured on the Programmable Fax
Application (`TELNYX_APP_ID`) in the Telnyx portal — the URL is *not* sent with
each fax, so it does not appear anywhere in this codebase. Point it at
`froide_fax-status_callback`. Requests are authenticated by their Ed25519
signature against `TELNYX_PUBLIC_KEY`.

Because that is the only signal that resolves a fax's `DeliveryStatus`, a lost
or misconfigured callback leaves a fax `SENDING` indefinitely. Schedule the
backstop sweep to catch that:

```python
CELERY_BEAT_SCHEDULE = {
    "sweep-pending-faxes": {
        "task": "froide_fax.tasks.sweep_pending_faxes",
        "schedule": crontab(minute=0, hour="*/4"),
    },
}
```

It re-checks any fax still `SENDING` after 30 minutes via
`GET /v2/faxes/{id}` and applies the result through the same code path as the
webhook.

The two intervals do different jobs. The 30-minute threshold defines *stuck* --
healthy callbacks land within seconds, so anything older has almost certainly
been lost. The sweep interval bounds how long a lost callback goes unnoticed.
Four-hourly is deliberate: this is a backstop for a broken webhook, not a
substitute for one, and polling more often mostly re-asks about faxes that are
simply slow. Pass `stale_minutes` to the task to override the threshold.

## The fax PDF is served over a signed, expiring URL

The rendered `fax.pdf` is stored as an unapproved `FoiAttachment`, so froide's
own auth refuses it to everyone but the requester. Telnyx, however, has to fetch
it without credentials, which `fax_media_url` does by streaming the file with
`authorized=True` behind a signed URL.

That signature now carries a timestamp and expires after an hour
(`FAX_MEDIA_URL_MAX_AGE`). Previously it was a plain `Signer` with no expiry, so
a URL handed to a third party -- and recorded in their access logs -- stayed
valid for as long as the signing key did.

Retries are unaffected: `retry_fax_delivery()` goes through `message.resend()`
into `run_send()`, which calls `get_media_url()` again and signs afresh.

The **status callback** URL deliberately keeps its non-expiring signature.
Delivery events can arrive long after the send, and expiring them would silently
drop statuses.

## Fax transport backends

`FAX_BACKEND` names the transport, the way `EMAIL_BACKEND` does for mail. It
defaults to the real one, so leaving it unset changes nothing:

```python
FAX_BACKEND = "froide_fax.backends.telnyx.TelnyxFaxBackend"   # default
FAX_BACKEND = "froide_fax.backends.console.ConsoleFaxBackend" # print, do not send
FAX_BACKEND = "froide_fax.backends.dummy.DummyFaxBackend"     # discard silently
```

The console and dummy backends exercise everything up to the wire -- the fax
`FoiMessage`, the rendered `fax.pdf` attachment, delivery status -- without a
Telnyx account or a network call. Useful for looking at the letter the
recipient would get.

Both record what they were asked to send, mirroring `django.core.mail.outbox`:

```python
from froide_fax.backends import outbox

outbox[0].to         # "+493012345678"
outbox[0].media_url  # signed URL of the rendered PDF
outbox[0].fax_id
```

They also answer `get_status()` with `delivered`, so the polling sweep can
resolve a message end to end. Without that a fax sent by a non-delivering
backend would sit in `STATUS_SENDING` for ever, since no webhook is coming.
They report no page count: nothing opened the PDF, and a fabricated number
would show up on the fax report.

Writing another backend means subclassing `BaseFaxBackend` and implementing
`send(to, media_url) -> FaxSendResult` and `get_status(fax_id) -> dict | None`,
where the dict is Telnyx-shaped.

## Testing

The test suite runs offline:

```
DJANGO_SETTINGS_MODULE=tests.settings DJANGO_CONFIGURATION=Test pytest tests/
```

Webhook payload fixtures are taken from Telnyx's published OpenAPI
description; see `tests/fixtures/telnyx/README.md`.

One thing cannot be covered offline: whether our webhook signature
verification interoperates with Telnyx's signing. `README_LIVE_TESTS.md`
describes how to record a real callback and switch those tests on.
