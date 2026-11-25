# 0029 — Conversation mode: talk to NOVA without saying its name every time

Date: 2026-11-25. Manav: "every time I need to tell something, I should start the sentence with Nova, which is kind
of awkward ... If I don't speak for a long time, then Nova can understand that okay, I'm not talking to it ... If it
is something out of the context, then it rejects that. If it is something within the context, then it understands
and corrects me if I'm wrong ... Just like a human."

## What changes (the always-open mics: laptop through NOVA Link, and the hub's own)
- **"Nova, ..." opens a conversation.** After each reply it stays open for `hub.conversation_s` (60 s); sentences in
  that window don't need the name. The face shows "listening" between replies so he can see it's still open.
- **Each nameless sentence is judged: for NOVA, or not?**
  - On the hub, instantly (`quick_verdict`): filler ("yeah", "hmm", "okay so") is never for NOVA; a question or a
    request-shaped sentence within 8 s of NOVA's reply is.
  - Everything unclear goes to `Judge`: a one-word YES/NO question to Claude on the API credit, with the last few
    lines of the conversation and the sentence fenced as data. Manav chose this over a hub-only model (2026-11-25):
    smarter, ~0.5 s, fractions of a cent per sentence, counted in the monthly budget (ADR 0026).
  - If the judge can't be asked (no key, budget used, network down, timeout 4 s, a non-YES/NO answer), the sentence
    is **not** for NOVA.
- **Not for NOVA → dropped exactly as before:** `{"ignored": true}`, never logged, stored, shown on the face or
  answered. NOVA keeps only counts of verdicts.
- **It closes** after the quiet time, or when he says a whole-sentence closer ("thanks", "that's all", "never mind",
  "bye"); NOVA says "Anytime." After that, the name is needed again.
- **When no conversation is open, nothing changes:** without the name nothing happens, and nothing is sent anywhere
  to be judged.
- **"Correct me if I'm wrong":** NOVA's prompt (API and Max) now says to tell him kindly, with the right answer, when
  he states something wrong instead of going along with it.

## Privacy change (accepted by Manav)
Before: speech not addressed to NOVA never left the hub. Now: **while a conversation is open**, a sentence the hub
can't decide on its own is sent to Anthropic's API to be judged (redacted like every brain request), and nothing is
kept. Idle NOVA sends nothing.

## Security
The judge only decides whether a sentence becomes a request -- what saying "Nova" already does for anything in the
room (a TV could always say "Nova, ..."). Every request still goes through the permission ladder as VOICE, so
risky actions still need a spoken "Nova, confirm". Hostile text in the sentence can at worst make the judge say YES.

## Settings
`hub.conversation_s: 60` (0 = off: every sentence needs the name, as before). The phone page and phone calls are
unchanged: everything said there is already for NOVA.

## Next
Knowing Manav's voice (speaker recognition) would let NOVA ignore other people in the room before anything is
judged; it is on the map as Exploring.
