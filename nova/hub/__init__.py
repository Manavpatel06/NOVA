"""nova-hub: NOVA's always-on home on the old laptop (docs/decisions/0019).

Runs `python -m nova.hub serve` as a systemd user service: a small local
model (Ollama) that answers simple commands before Claude is asked, and a
mirror of NOVA's memory. Linux only; nothing here is imported by the laptop
app."""
