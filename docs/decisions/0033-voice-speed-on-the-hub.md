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
