# 0026 — The hub thinks with the monthly API credit first

Date: 2026-11-25. Manav: "I have monthly $100 credit, we can use that first before the Max plan, maybe for faster
answers."

## Decision
`hub.brain: api_first` (the default):
- The hub's NOVA answers with the Claude API (`nova.brain.agent.Agent`, Haiku 5.5, prompt caching on the system
  prompt and tool list) while a key is set and the month's estimated spend is under `brain.api_budget_usd` (90).
- Otherwise it uses the Max plan conversation, as before (`FirstAvailable([api, max])`).
- If the API can't be reached before anything happened, the same sentence goes to Max.
- Instant commands are noted to both brains, so either can follow up.

## Money (`nova/brain/spend.py`)
- **Prices.** Haiku 5.5 list prices (2026-11-25): $0.10/MTok input, $0.125/MTok 5-minute cache writes,
  $0.01/MTok cache reads, $0.50/MTok output. Prompts over 100k tokens cost 5×. Web search is $10 per 1,000
  searches.
- **Defaults on the safe side.** An unknown model is charged at a high rate, never zero.
- **How spend is tracked.**
  - Every API reply's `usage` is added to `data/api_spend.json` for the calendar month.
  - Anthropic's "credit balance is too low" error marks the month as used up immediately; the Max plan then
    answers until the 1st.
- **The total is an estimate.** No billing API is read, which is why the budget is $90, not $100.
- **A typical voice turn** (about 6k cached tokens and 200 output tokens) costs about $0.0003, so the credit lasts
  far beyond NOVA's use. Web searches are the only cost that adds up.

## Key
- On the hub, `data/anthropic_key` is written 0600 by `nova-hub apikey`. Manav pastes the key with hidden input;
  it goes through a pipe and is proven with one tiny request.
- A key file readable by other accounts is ignored.
- Windows keeps using Credential Manager.
- `api.anthropic.com` joins the hub's egress allowlist.

## Speed
Max on the hub measured about 1.5 s per short turn. The API is expected to be somewhat faster, but that isn't
measured yet (no key in the build environment). Most of a spoken turn's time is listening and speaking, not
Claude. `nova-hub status` shows the month's spend.
