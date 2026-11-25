"""NOVA's phone number on Plivo (2026-11-25): the cheaper carrier, same gate --
the carrier's signature, only NOVA's and Manav's numbers, the PIN first."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import stat
import os

import pytest

from nova.hub.phone import Phone, PhoneError, plivo_signature, write_settings

NAME = "nova-hub.tail1.ts.net"
AUTH_ID = "MAABCDEFGHIJ12345678"
TOKEN = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZg"
NOVA = "+14805551234"
ME = "+14805550000"
CALL = "4f1c2e3a-1b2c-4d5e-8f90-0a1b2c3d4e5f"
BASE = f"https://{NAME}:8443/phone"


class _Talk:
    def __init__(self):
        self.said = []

    def phone_say(self, sid, heard, wait_s):
        self.said.append(heard)
        return "It's 7 PM.", False

    def phone_wait(self, sid, wait_s):
        return "Done.", False


class _Egress:
    def __init__(self):
        self.posts = []

    def post_json(self, url, body, *, headers, timeout=20.0):
        self.posts.append((url, body, headers))
        return {"message": "call fired", "request_uuid": "x"}


def _phone(tmp_path, talk=None, egress=None):
    write_settings(tmp_path / "phone.json", provider="plivo", account_sid=AUTH_ID, auth_token=TOKEN,
                   nova_number=NOVA, owner_number=ME, pin="123456")
    return Phone(talk or _Talk(), data_dir=tmp_path, hub_name=lambda: NAME, egress=egress)


def _hook(phone, route, form, query="", nonce="12345678901234567890", sig=None, numbers=("14805550000", "14805551234")):
    form = {"CallUUID": CALL, "From": numbers[0], "To": numbers[1], **form}
    url = f"{BASE}/{route}" + (f"?{query}" if query else "")
    headers = {"x-plivo-signature-v3": sig if sig is not None else plivo_signature(TOKEN, url, form, nonce),
               "x-plivo-signature-v3-nonce": nonce}
    return phone.webhook(route, query, form, headers)


def test_signature_matches_plivos_documented_construction():
    # URL, ".", the POST params sorted by name as name+value, ".", the nonce; HMAC-SHA256, base64
    form = {"Digits": "1234", "To": "+15555555555", "From": "+15551111111",
            "CallUuid": "4vbcpem8-0u46-x1ha-9af1-438vc92bf374"}
    url = "https://example.com/abcd?foo=bar"
    payload = ("https://example.com/abcd?foo=bar.CallUuid4vbcpem8-0u46-x1ha-9af1-438vc92bf374"
               "Digits1234From+15551111111To+15555555555.nonce1")
    expected = base64.b64encode(hmac.new(b"tok", payload.encode(), hashlib.sha256).digest()).decode()
    assert plivo_signature("tok", url, form, "nonce1") == expected


def test_a_plivo_call_answers_with_plivo_xml_and_asks_for_the_pin(tmp_path):
    status, xml = _hook(_phone(tmp_path), "voice", {})
    assert status == 200 and "<Speak" in xml and "<GetInput" in xml and 'inputType="dtmf"' in xml
    assert "<Say" not in xml and "<Gather" not in xml and f'action="{BASE}/pin"' in xml


def test_unsigned_or_wrongly_signed_requests_are_refused(tmp_path):
    p = _phone(tmp_path)
    assert _hook(p, "voice", {}, sig="")[0] == 403
    assert _hook(p, "voice", {}, sig="bm9wZQ==")[0] == 403
    assert p.webhook("voice", "", {"CallUUID": CALL, "From": "14805550000", "To": "14805551234"},
                     {"x-twilio-signature": "anything"})[0] == 403   # Twilio's header means nothing here


def test_one_of_several_comma_separated_signatures_is_enough(tmp_path):
    p = _phone(tmp_path)
    form = {"CallUUID": CALL, "From": "14805550000", "To": "14805551234"}
    good = plivo_signature(TOKEN, f"{BASE}/voice", form, "n1")
    assert p.webhook("voice", "", form, {"x-plivo-signature-v3": f"bm9wZQ==, {good}",
                                         "x-plivo-signature-v3-nonce": "n1"})[0] == 200


def test_only_manavs_and_novas_numbers_with_or_without_plus(tmp_path):
    p = _phone(tmp_path)
    assert "<Speak" in _hook(p, "voice", {}, numbers=("+14805550000", "+14805551234"))[1]
    status, xml = _hook(p, "voice", {}, numbers=("19998887777", "14805551234"))
    assert status == 200 and 'reason="rejected"' in xml


def test_the_pin_then_speech_reaches_nova(tmp_path):
    talk = _Talk()
    p = _phone(tmp_path, talk)
    assert "Enter your PIN first" in _hook(p, "heard", {"Speech": "what time is it"})[1]
    assert talk.said == []
    xml = _hook(p, "pin", {"Digits": "123456", "InputType": "dtmf"})[1]
    assert "Hi Manav" in xml and 'inputType="speech"' in xml and f'action="{BASE}/heard"' in xml
    xml = _hook(p, "heard", {"Speech": "what time is it", "InputType": "speech"})[1]
    assert talk.said == ["what time is it"]
    assert "7 PM." in xml and "<Speak" in xml


def test_call_ids_must_be_plivo_uuids(tmp_path):
    p = _phone(tmp_path)
    assert _hook(p, "voice", {"CallUUID": "CA" + "a" * 32})[0] == 400


def test_nova_calls_manav_through_plivos_api(tmp_path):
    eg = _Egress()
    p = _phone(tmp_path, egress=eg)
    assert p.call_me("Time to wake up.")["ok"]
    url, body, headers = eg.posts[0]
    assert url == f"https://api.plivo.com/v1/Account/{AUTH_ID}/Call/"
    assert body["from"] == "14805551234" and body["to"] == "14805550000"
    assert body["answer_url"].startswith(f"{BASE}/voice?msg=") and body["hangup_url"] == f"{BASE}/status"
    assert headers["authorization"] == "Basic " + base64.b64encode(f"{AUTH_ID}:{TOKEN}".encode()).decode()
    token = body["answer_url"].split("msg=")[1]
    xml = _hook(p, "voice", {}, query=f"msg={token}")[1]
    assert "Time to wake up." in xml


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_settings_are_private_and_name_the_carrier(tmp_path):
    _phone(tmp_path)
    data = json.loads((tmp_path / "phone.json").read_text())
    assert data["provider"] == "plivo" and "pin" not in data
    assert stat.S_IMODE((tmp_path / "phone.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("field,value", [("account_sid", "AC" + "a" * 32), ("auth_token", "short"),
                                         ("provider", "nexmo")])
def test_bad_plivo_settings_are_refused(tmp_path, field, value):
    args = dict(provider="plivo", account_sid=AUTH_ID, auth_token=TOKEN, nova_number=NOVA, owner_number=ME,
                pin="123456")
    args[field] = value
    with pytest.raises(PhoneError):
        write_settings(tmp_path / "phone.json", **args)


def test_old_twilio_settings_still_work_without_a_provider(tmp_path):
    from tests.test_hub_phone import _hook as twilio_hook, _phone as twilio_phone, _settings

    _settings(tmp_path)
    data = json.loads((tmp_path / "phone.json").read_text())
    data.pop("provider", None)
    (tmp_path / "phone.json").write_text(json.dumps(data))
    status, xml = twilio_hook(twilio_phone(tmp_path), "voice", {})
    assert status == 200 and "<Say" in xml
