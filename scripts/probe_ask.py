"""Ask NOVA on the hub one typed sentence and print the reply and the time (diagnostics)."""
import sys
import secrets
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nova.config import load_config  # noqa: E402
from nova.security.egress import Egress  # noqa: E402


class _NoAudit:
    def record(self, entry):
        pass


config = load_config()
host = config.hub.host.strip().lower()
eg = Egress(list(config.security.egress_allowlist) + [host], audit=_NoAudit(), redact_terms=config.security.redact_terms)
t = time.monotonic()
out = eg.post_json(f"https://{host}/v1/talk", {"session": "ask-" + secrets.token_hex(5), "text": sys.argv[1],
                                               "speak": False}, headers={}, timeout=120)
print(f"{time.monotonic() - t:.2f}s  {out.get('say')}")
