# Live tests against Telnyx

Everything in `tests/` runs offline. This file covers the parts that cannot:
capturing a real webhook callback, and the checks that only become possible
once you have one.

There is exactly one thing in this package that no offline test can cover:
**whether our signature verification interoperates with Telnyx's signing.**
Every signature test in `tests/test_webhook.py` generates a keypair, signs with
it and verifies -- nacl agreeing with nacl. That proves internal consistency
and nothing else. No public fixture can substitute, because a signature is only
meaningful against the key that produced it.

Closing that gap costs one fax and about twenty minutes.

## What you need

A Telnyx account with:

- an API key
- one phone number (a US one is fine and costs about $1/month -- Austrian and
  German numbers need in-country address proof and take ~72 hours to activate,
  which buys nothing here)
- your account public key, from Mission Control -> Keys & Credentials ->
  Public Key
- a Programmable Fax Application

## Why the capture endpoint comes first

`webhook_event_url` is **required** when you create the Fax Application, and it
must be a reachable HTTPS URL. So the endpoint has to exist before the
application does.

Two ways to provide one.

### Option A: a tunnel to your dev server (preferred)

```
cloudflared tunnel --url http://localhost:8000
```

Nothing leaves your machine, and you can point the application straight at the
real `/fax/fax-callback/`, so signature verification, the replay window and
`DeliveryStatus` all run for real. If the dev server is up, use this.

### Option B: webhook.site

Open <https://webhook.site>, copy the unique URL it gives you. Every request
sent there is captured and displayed: method, headers, raw body. No account, no
deployment.

> **Never point this at anything carrying a real request.** The payload
> contains `original_media_url`, which for a real fax is the signed
> `froide_fax-media_url` -- a capability URL that lets whoever holds it fetch
> the requester's PDF. webhook.site URLs are unguessable but unauthenticated.
> Test faxes only, and repoint the application before any real traffic.

## Recording a capture

1. Create the capture endpoint (above).

2. Create the Fax Application with it:

   ```
   curl -X POST https://api.telnyx.com/v2/fax_applications \
     -H "Authorization: Bearer $TELNYX_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"application_name": "froide-fax capture",
          "webhook_event_url": "https://<your-capture-url>"}'
   ```

   Keep the returned `id`; that is `TELNYX_APP_ID` / `connection_id`.

   The URL is not fixed forever -- `PATCH /v2/fax_applications/{id}` changes it.
   But prefer a second application for production over repointing this one, so
   capture experiments never redirect live callbacks. `tasks.send_test_fax`
   already assumes that split with its `faxtest_app_id` setting.

3. Send one fax. <https://faxbeep.com> answers for free, so it produces a real
   `fax.delivered`:

   ```
   curl -X POST https://api.telnyx.com/v2/faxes \
     -H "Authorization: Bearer $TELNYX_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"connection_id": "<app id>",
          "to": "<faxbeep number>",
          "from": "<your number>",
          "media_url": "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"}'
   ```

   Send a second to an unallocated number for a real `fax.failed` and a real
   `failure_reason`.

4. Collect, for the callback you want to keep:

   - the `telnyx-timestamp` header
   - the `telnyx-signature-ed25519` header
   - **the exact raw body bytes**

   The signature is computed over `{timestamp}|{raw body}`. One re-indented
   character or changed newline and a valid signature fails. Fetch the body
   through webhook.site's API, or log it from the tunnel -- do not copy it out
   of a rendered browser view. This is the single most common way to lose an
   afternoon here.

5. Write `tests/fixtures/telnyx/captured_callback.json`:

   ```json
   {
     "public_key": "<account public key, base64>",
     "timestamp": "1746441600",
     "signature": "<telnyx-signature-ed25519 header, base64>",
     "body_base64": "<base64 of the exact raw body bytes>"
   }
   ```

   The body is base64-encoded precisely so it survives being stored in JSON
   without re-serialisation changing a byte.

## Running the tests

Nothing to enable. `TestCapturedSignature` in `tests/test_webhook.py` skips
while the file is absent and activates once it exists:

```
DJANGO_SETTINGS_MODULE=tests.settings DJANGO_CONFIGURATION=Test \
  pytest tests/test_webhook.py -k TestCapturedSignature
```

Three checks run:

- the real signature is accepted (proves interop)
- the same signature against a mutated body is rejected (proves verification is
  actually running, rather than passing everything)
- the envelope is recorded as it appeared on the wire

The capture is older than the five-minute replay window, so the tests move the
clock back to the moment Telnyx signed it. That is why they need `time_machine`
and cannot simply post the payload.

The file is gitignored: it carries your account id, the fax numbers involved
and your public key. None of that is a credential, but none of it needs to be
in the repository either. If you decide to share it, `git add -f`.

## What this still does not cover

- **The media pull.** Telnyx fetches the PDF from `froide_fax-media_url`
  itself, and does not follow redirects. Only a publicly reachable deployment
  exercises that.
- **The callback URL configuration.** It lives on the Fax Application in the
  portal, not in this repository. Nothing here can detect it being wrong.
- **A misconfigured callback now fails quietly.** Malformed or missing
  signature headers, and a wrong `TELNYX_PUBLIC_KEY`, return 403 rather than
  500. When wiring up production, grep the access log for 403s on the callback
  URL; absence of 500s is not evidence it is working.
- **Clock skew.** The replay window compares Telnyx's timestamp to server time.
  A drifting clock rejects every callback. Check NTP before blaming the code.

The polling sweep is the backstop for all of these: it resolves faxes stuck in
`SENDING` when a callback never arrives. Leave it enabled while you are still
debugging the webhook.
