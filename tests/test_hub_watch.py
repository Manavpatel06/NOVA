"""NOVA warns first (2026-11-25): power, heat, disk, and a morning Canvas heads-up."""
from __future__ import annotations

from datetime import datetime

from nova.hub.watch import Watch, count_due

CANVAS = """Upcoming Assignments (Next 2 Days):

• <<assignment title>>HW 5<</>>
  Course: CSE 576
  Due: Nov 26
  Status: Not submitted

• Quiz 3
  Course: CSE 575
  Due: Nov 27
  Status: Submitted
"""


class _Conn:
    def __init__(self, text=CANVAS, ready=True):
        self.text, self.ready, self.calls = text, ready, []

    def status(self):
        return [{"name": "canvas", "state": "ready" if self.ready else "failed"}]

    def call(self, name, tool, args):
        self.calls.append((name, tool, args))
        return self.text, False


def _watch(facts, **kw):
    t = {"now": 0.0}
    said = []
    w = Watch(said.append, facts=lambda: facts[0], clock=lambda: t["now"], **kw)
    return w, t, said


def test_unplugged_warns_after_two_minutes_then_low_then_back():
    facts = [{"battery_status": "discharging", "battery_pct": 60}]
    w, t, said = _watch(facts, heads_up_hour=-1)
    w.tick()
    t["now"] = 60
    w.tick()
    assert said == []                                   # a blip isn't worth a ping
    t["now"] = 130
    w.tick()
    assert said == ["The hub is running on its battery (60 percent). Is it unplugged?"]
    facts[0] = {"battery_status": "discharging", "battery_pct": 18}
    w.tick()
    facts[0] = {"battery_status": "discharging", "battery_pct": 17}
    w.tick()
    assert said[-1].startswith("The hub's battery is down to 18 percent") and len(said) == 2
    facts[0] = {"battery_status": "discharging", "battery_pct": 9}
    w.tick()
    assert said[-1].startswith("The hub's battery is down to 9 percent")
    facts[0] = {"battery_status": "charging", "battery_pct": 9}
    w.tick()
    assert said[-1] == "The hub is plugged in again." and len(said) == 4


def test_a_charging_hub_says_nothing():
    w, t, said = _watch([{"battery_status": "charging", "battery_pct": 50, "temp_c": 55, "disk_free_gb": 300}],
                        heads_up_hour=-1)
    for i in range(5):
        t["now"] = i * 60
        w.tick()
    assert said == []


def test_heat_needs_two_readings_and_repeats_at_most_hourly():
    w, t, said = _watch([{"temp_c": 95}], heads_up_hour=-1)
    w.tick()
    assert said == []
    w.tick()
    assert said == ["The hub is running hot (95 degrees). Is its vent blocked?"]
    t["now"] = 600
    w.tick()
    assert len(said) == 1


def test_disk_nearly_full_once_a_day():
    w, t, said = _watch([{"disk_free_gb": 3}], heads_up_hour=-1)
    w.tick()
    w.tick()
    assert said == ["The hub has only 3 gigabytes free."]


def test_morning_heads_up_counts_only_and_once_a_day(tmp_path):
    conn = _Conn()
    now = [datetime(2026, 11, 26, 7, 59)]
    said = []
    w = Watch(said.append, facts=dict, connectors=conn, state_path=tmp_path / "watch.json", heads_up_hour=8,
              now=lambda: now[0])
    w.tick()
    assert said == [] and conn.calls == []             # not yet 8
    now[0] = datetime(2026, 11, 26, 8, 1)
    w.tick()
    w.tick()
    assert said == ['Canvas: 1 thing due in the next 2 days (1 already submitted). Ask NOVA "what\'s due?" for details.']
    assert "HW 5" not in said[0]                       # titles never go through Bark/Apple
    assert conn.calls == [("canvas", "get_my_upcoming_assignments", {"days": 2})]
    w2 = Watch(said.append, facts=dict, connectors=conn, state_path=tmp_path / "watch.json", heads_up_hour=8,
               now=lambda: now[0])
    w2.tick()
    assert len(said) == 1                              # a restart doesn't repeat it


def test_no_heads_up_without_canvas_or_when_nothing_is_due(tmp_path):
    for conn in (_Conn(ready=False), _Conn(text="No assignments due in the next 2 days.")):
        said = []
        Watch(said.append, facts=dict, connectors=conn, heads_up_hour=0,
              now=lambda: datetime(2026, 11, 26, 9)).tick()
        assert said == []


def test_count_due():
    assert count_due(CANVAS) == (2, 1)
    assert count_due("No assignments due in the next 2 days.") == (0, 0)
