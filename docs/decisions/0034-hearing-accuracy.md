# 0034 — Hearing accuracy: Manav's own words

Date: 2026-11-27. Manav: "let's not worry about delay anymore, let's focus on making NOVA strong and accurate."

## Measured
60 NOVA-style commands (30 sentences x 2 of 6 Kokoro voices, incl. two Hindi-accented, with room reverb and fan
noise), Whisper small.en int8 on CPU, scored on word errors and on whether the words that matter were heard:

| setting | word errors | key words heard |
|---|---|---|
| beam 1, hotwords "Nova Manav" (before) | 7.1 % | 91.9 % |
| beam 5, hotwords "Nova Manav" | 7.1 % | 92.5 % |
| beam 5, his words as hotwords | 5.1 % | 98.1 % |
| beam 1, his words as the prompt | 3.4 % | 97.5 % |
| **beam 5, his words as the prompt (now)** | **2.8 %** | **97.5 %** |
| ... after "Nova, learn the word Ghosh" | 1.7 % | 100 % |

Misses before: HydraWav3 -> "Hydra Vav3", Tempe -> "intent", ARC -> "Ark", lofi -> "Loafy", Ghosh -> "Gosh".
False "Nova": 0 of 10 sentences not meant for NOVA and 0 of 10 noise clips, with every setting.
Cost: beam 5 is ~0.4 s slower per sentence (Manav: accuracy over speed).

## What NOVA does now
- `nova/hub/vocabulary.py` builds the prompt: NOVA's names, Manav, Tempe, ASU, Canvas, Gmail, **his own words**
  (`data/vocabulary.txt`), his Canvas course codes (read twice a day when Canvas is connected), his app names and his
  connected-account labels. Capped at 700 characters, his words first.
- Hub skills: **learn_word** ("Nova, learn the word Ghosh" -- ACT), **forget_word**, **my_words**.
- A word list that can't be read never stops hearing: it falls back to the old hotwords.
- `scripts/eval_hearing.py` runs the same kind of check on the hub with its real settings and word list.
