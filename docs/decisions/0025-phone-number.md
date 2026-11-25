# 0025 — A real phone number for NOVA

Date: 2026-11-25. Phase 4 of ADR 0023. Manav wanted to give NOVA a phone number: call it, and have it call him.
He accepted about $1 a month plus per-minute charges.

## Shape
- **Twilio carries the call.** Its `<Gather input="speech">` does speech recognition and `<Say>` reads replies,
  so there are no media streams and no websockets.
  - Call audio and the words passing through it go through Twilio. That is the price of a real number, and it
    applies to calls only.
  - Manav creates the account, buys the number and runs `nova-hub phone`. NOVA never signs up for anything.
- **The internet reaches the hub only through Tailscale Funnel**, on port 8443, mounted at `/phone` →
  `127.0.0.1:8765/v1/phone`. Nothing else is exposed.
- **What every webhook passes, in order (`nova/hub/phone.py`):**
  1. Twilio's signature: HMAC-SHA1 over the exact public URL plus the sorted form fields, with Manav's auth
     token, compared in constant time. It matches the test vector Twilio's own libraries use
     (`0/KCTR6DLpKmkAf8muzZqo1nDgQ=`).
  2. The call is between NOVA's number and Manav's number. Anything else gets `<Reject/>`.
  3. A 6-digit PIN typed on the keypad comes before anything NOVA does, because caller ID can be spoofed.
     - It is stored as PBKDF2-SHA256 (200k rounds) with a salt, never in plain text.
     - Five wrong PINs lock calls for an hour.
     - A finished call forgets its PIN.
- **Defense in depth at the listener.**
  - A request carrying `Tailscale-Funnel-Request` is never the owner, and can reach nothing but
    `/v1/phone/(voice|pin|heard|wait|status)`.
  - Phone routes also check Host, content type and size (16 KB).
  - Placing a call (`/v1/phone/call`) is owner-only JSON, like every route.
- **On a call.**
  - Turns are `ArmedBy.VOICE`: risky actions still need "Nova, confirm".
  - Replies are XML-escaped.
  - Twilio's 15 s webhook limit is handled with "One moment" and a redirect to `/phone/wait`.
- **NOVA calls Manav.**
  - `call_me` (a hub skill, ACT) or `remind_me(..., call='yes')`, which phones once when the reminder is due,
    alongside Bark.
  - The message stays on the hub behind a one-time token; it never travels in the URL Twilio sees.
  - Whoever answers hears the message. Talking to NOVA after that still needs the PIN.
- **Settings.** `data/phone.json` (0600) is written by `nova-hub phone`, which reads every value with `read`
  (the token and PIN hidden) and hands them over through a pipe, never on a command line. Egress uses the
  sanctioned `Egress.post_form`: allowlisted `api.twilio.com`, audited without bodies or headers.

## To verify on the real hub
- Tailscale's `--set-path` mapping. `nova-hub phone` posts an unsigned test call to its own public URL and
  expects 403 from NOVA's signature check.
- Twilio's speech recognition quality and the round-trip time on a real call.

## Update 2026-11-25: Plivo as well as Twilio
Manav asked for the cheapest number. Plivo: a US number is about $0.50 a month, calls about half a cent a minute
inbound and a cent outbound, and the trial gives free credit without a card. `nova-hub phone` now asks which carrier
(Plivo first). The gate is unchanged: Plivo's `X-Plivo-Signature-V3` (HMAC-SHA256 of the URL, "." and the form sorted by
name as name+value, "." and the nonce, with the Auth Token; any of several comma-separated signatures), then only NOVA's
and Manav's numbers (compared as digits, since Plivo may leave off the "+"), then the PIN. Plivo call ids are UUIDs; its
XML uses `<Speak>`, `<GetInput>` (dtmf for the PIN, speech after) and `<Wait>`. NOVA calls out with
`POST https://api.plivo.com/v1/Account/{auth_id}/Call/` (answer and hangup URLs on the Funnel route), and
`api.plivo.com` joins the hub's egress allowlist. Settings without a carrier stay Twilio.
