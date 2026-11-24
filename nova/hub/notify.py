"""Making Manav's iPhone ring: Bark push notifications (2026-11-24).

Bark (github.com/Finb/Bark) is a free, open-source iPhone app. The hub POSTs
to Bark's server (api.day.app, the only host the hub's Egress allows), which
hands the notification to Apple's push service.

Privacy: Bark's server and Apple see what we send, so by default the
notification only says "Reminder -- tap to see it". Tapping opens the hub's
own page over Tailscale (https://<hub>/v1/ack/...), which shows the text and
marks it done. `hub.alert_text: true` puts the text in the notification
itself instead.

Urgent reminders use Bark's "critical" level ("ignore[s] silent and do not
disturb modes", needs the Critical Alerts permission in the Bark app) and
call=1 (rings for 30 seconds); the hub repeats them until acknowledged.

The device key is a secret: kept in data/bark_key (mode 600, written by
`nova-hub bark <key>`), never logged or audited.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from nova.log import get_logger

_log = get_logger("hub.notify")

PUSH_URL = "https://api.day.app/push"
HOST = "api.day.app"


class Bark:
    def __init__(self, egress: Any, *, key_path: Path, hub_name: Callable[[], str], show_text: bool = False) -> None:
        self._egress = egress
        self._key_path = Path(key_path)
        self._hub_name = hub_name
        self._show_text = show_text

    def key(self) -> str:
        try:
            return self._key_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def configured(self) -> bool:
        return bool(self.key())

    def payload(self, reminder: dict[str, Any], *, late: bool = False) -> dict[str, Any]:
        urgent = bool(reminder.get("urgent"))
        if self._show_text:
            body = reminder["text"]
        else:
            body = ("Urgent reminder" if urgent else "Reminder") + " -- tap to see it"
        if late:
            body = "(late) " + body
        out: dict[str, Any] = {
            "device_key": self.key(),
            "title": "NOVA",
            "body": body,
            "group": "nova",
            "level": "critical" if urgent else "timeSensitive",
        }
        if urgent:
            out.update(call="1", volume="8")
        name = self._hub_name()
        if name and reminder.get("id") and reminder.get("token"):
            out["url"] = f"https://{name}/v1/ack/{reminder['id']}?t={reminder['token']}"
        return out

    def send(self, reminder: dict[str, Any], late: bool = False) -> bool:
        """Deliver one notification; False (try again next tick) on any failure."""
        if not self.configured():
            return False
        try:
            self._egress.post_json(PUSH_URL, self.payload(reminder, late=late), headers={}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 -- retried on the next tick
            _log.warning("Bark push failed (%s)", type(exc).__name__)
            return False
        _log.info("Bark push sent (%s)", "urgent" if reminder.get("urgent") else "normal")
        return True

    def test(self) -> bool:
        """One plain notification, to check the key and the Bark app."""
        if not self.configured():
            return False
        body = {"device_key": self.key(), "title": "NOVA", "body": "NOVA can reach you.",
                "group": "nova", "level": "timeSensitive"}
        try:
            self._egress.post_json(PUSH_URL, body, headers={}, timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            _log.warning("Bark test failed (%s)", type(exc).__name__)
            return False
        return True
