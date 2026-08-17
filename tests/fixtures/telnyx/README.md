# Telnyx webhook fixtures

Verbatim `example` blocks from the fax webhook schemas in Telnyx's published
OpenAPI description, `team-telnyx/openapi`, `openapi/spec3.json`:

| File | Schema | event_type |
| --- | --- | --- |
| fax_queued.json | FaxQueued | fax.queued |
| fax_media_processed.json | FaxMediaProcessed | fax.media.processed |
| fax_sending_started.json | FaxSendingStarted | fax.sending.started |
| fax_delivered.json | FaxDelivered | fax.delivered |
| fax_failed.json | FaxFailed | fax.failed |

Copied unmodified so drift is visible in a diff. Prefer these over the HTML
documentation: the docs page renders fax.delivered and fax.failed without the
`data`/`meta` envelope, which contradicts every schema here and appears to be a
rendering fault.

These carry no signature. A webhook signature is only meaningful against the
key that produced it, so covering that needs one real captured callback.
