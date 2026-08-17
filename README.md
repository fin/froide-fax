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
        "schedule": crontab(minute="*/15"),
    },
}
```

It re-checks any fax still `SENDING` after 30 minutes via
`GET /v2/faxes/{id}` and applies the result through the same code path as the
webhook.
