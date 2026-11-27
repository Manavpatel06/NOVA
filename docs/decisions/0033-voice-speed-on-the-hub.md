# 0033 — Voice speed on the hub

Date: 2026-11-26. Manav: "voice input is delayed and lagging in output as well."

## Measured (laptop -> hub, `scripts/probe_voice_latency.py`)
- Network: direct over the home network, but ~110 ms a round trip (Wi-Fi power saving on the hub).
- Speech-to-text on the hub: 7.9 s for 1.8 s of speech. The same model and code on a 2-core cloud machine: 2.1 s
  for 2.9 s of speech.
- The voice: 9.8 s for one short sentence (cloud: 1.8 s).
- Claude itself: about 2 s, fine. Hub load average 2.9 with nobody talking; 75 C.

So the hub's CPU was busy with something else. The likeliest: the full-screen face, redrawing at the display's
rate. Measured with WebKit: the face's cost follows the number of frames drawn, not what's in them (an empty
full-screen canvas at 60 fps costs as much as the nova).

## Changes
- **The face gives way**:
  - It runs at low CPU priority (`os.nice(15)`; WebKit's processes inherit it).
  - Frame rate is capped by mode: idle 10, listening 20, thinking 8 (when NOVA needs the CPU most),
    speaking 20, offline 4.
  - Half the filaments, the star field drawn once, a 0.75 drawing scale.
  - Measured in a browser: drawing calls per frame went from about 9,000 to 3,900, and the frame rate is capped.
    Before, the face drew as many frames as the machine could manage.
- **Speech-to-text**: greedy decoding (beam 1) and no timestamps (~10 % faster measured), hotwords "Nova Manav".
- **`nova-hub tune`** (asks y/n for each; `tune off` undoes): Wi-Fi power saving off, CPU performance profile
  while plugged in.
- **Timings**: every turn logs `hear / think / voice / total` ms on the hub (never the words); a talk request
  with `"timing": true` gets them back. The probe uses this.

## To verify on the hub
Run `nova-hub update`, `nova-hub tune`, sign out and back in (the face restarts with the new settings), then
`scripts/probe_voice_latency.py` from the laptop. Expected: hearing ~2 s, first voice ~2 s.

## After the first fix (measured 2026-11-26, laptop -> hub)
- Speech-to-text 7.9 s -> 1.9 s; first piece of voice 9.8 s -> 1.9 s. The face was the CPU hog.
- Still slow: a new HTTPS connection for every request, 187 ms each (22 ms reusing one). Now **kept open**
  (`Egress(keep_alive_hosts=...)`): NOVA Link -> the hub, and the hub -> Claude's API. Same rules: allowlisted
  hosts only, certificate checked, redirects refused, every request audited. A connection the server closed while
  idle is replaced once.
- "Nova, ..." typed or said on the phone page went to Claude even for "what time is it". The name is now
  stripped first, so simple commands take the instant lane there too.
- **`nova-hub hearing fast|accurate`**: Whisper base.en is 2.5x quicker than small.en (0.8 s vs 2.1 s per
  sentence measured) but makes more mistakes ("Nova, open the folder" -> "Nova opened the folder"). small.en
  stays the default.
- Tried and dropped: the int8 Kokoro model (no faster on this CPU: 3.44 s vs 3.57 s).

## Speaking while Claude is still writing (2026-11-26)
- The API brain now reads Claude's reply as it streams (`Egress.post_stream`: same allowlist, audit, redirect and
  error rules; kept-open connection). Each time more words arrive it reports the reply so far.
- The hub voices every piece that can no longer change (at most two ahead) while the rest is still being written,
  so when the reply is finished its first piece is usually ready. Nothing is ever voiced twice; a piece that isn't
  in the final reply (text before a tool call) is just not used. Only when the reply will be spoken here (not on
  phone calls, which the carrier voices).
- The gain is for replies of two or more sentences (longer answers from the stronger model most of all). A
  one-sentence reply can't be voiced before it's complete, so it gains nothing.
- The reply format and the laptop, page and phone are unchanged; the Max plan brain doesn't stream.
- Measured before this on the hub: a three-sentence answer took 3.3-4.5 s just to voice its first piece, because
  a long first sentence was one piece. A first sentence over 90 characters is now broken at its last comma,
  semicolon, colon or dash between 25 and 90 characters (left whole when there's none).
- Measured after it (hub): a three-sentence answer is ready to play in 2.9-3.6 s (was 5.6-7.2 s).
- Found then: a sentence said right after a long answer waited 2.5-3 s for speech-to-text, behind the voicing of
  that answer's later pieces (one lock for both). Hearing and speaking now have their own locks, and the later
  pieces of a reply wait while someone is being heard (`HubVoice.speak_later`). Tested.
