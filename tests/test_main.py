import asyncio
import hashlib
import hmac
import importlib
import json

import pytest
from fastapi.testclient import TestClient

# Fake numbers. Real ones are in international format, country code first, no "+".
SENDER = "10001"
STRANGER = "10002"


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("VERIFY_TOKEN", "vt")
    monkeypatch.setenv("WA_APP_SECRET", "secret")
    monkeypatch.setenv("ALLOWED_WA_NUMBERS", "+" + SENDER)
    import main
    importlib.reload(main)
    dispatched = []
    monkeypatch.setattr(main, "dispatch", dispatched.append)
    return main, TestClient(main.app), dispatched


def post(client, payload, secret="secret"):
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})


def payload(sender, msg_id="m1"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": msg_id, "type": "audio", "audio": {"id": "a1", "mime_type": "audio/ogg"}}
    ]}}]}]}


def test_verify(bot):
    _, c, _ = bot
    ok = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no"}).status_code == 403


def test_bad_signature_dropped(bot):
    _, c, dispatched = bot
    assert post(c, payload(SENDER), secret="wrong").status_code == 403
    assert dispatched == []


def test_only_allowed_number_and_no_duplicates(bot):
    _, c, dispatched = bot
    post(c, payload(STRANGER))
    post(c, payload(SENDER, "m1"))
    post(c, payload(SENDER, "m1"))
    assert [m["id"] for m in dispatched] == ["m1"]


def test_wa_allowlist_parsing(bot):
    main, _, _ = bot
    assert main.parse_numbers(" +10001, 10 003 ,,") == {"10001", "10003"}
    assert main.allowed_number(SENDER)
    assert not main.allowed_number(STRANGER)
    assert not main.allowed_number("")


def test_empty_wa_allowlist_answers_nobody(monkeypatch):
    monkeypatch.setenv("WA_APP_SECRET", "secret")
    monkeypatch.setenv("ALLOWED_WA_NUMBERS", "")
    import main
    importlib.reload(main)
    dispatched = []
    monkeypatch.setattr(main, "dispatch", dispatched.append)
    post(TestClient(main.app), payload(SENDER))
    assert dispatched == []
    assert not main.allowed_number(SENDER)


def test_split_text(bot):
    main, _, _ = bot
    assert main.split_text("hello") == ["hello"]
    long = ("word " * 2000).strip()
    parts = main.split_text(long)
    assert len(parts) == 3
    assert parts[0].startswith("(1/3)\n")
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(p.split("\n", 1)[1].replace(" ", "") for p in parts) == long.replace(" ", "")
    assert main.txt_bytes("سلام").decode("utf-8").startswith("﻿سلام")


@pytest.fixture
def wired(monkeypatch):
    """main with every network call faked; records what would be sent."""
    import main
    importlib.reload(main)
    out = []

    async def fake_read(client, msg_id, typing=True):
        pass

    async def fake_send(client, to, text, reply_to=""):
        out.append(("text", text, reply_to))

    async def fake_send_voice(client, to, audio, reply_to=""):
        out.append(("voice", audio, reply_to))

    async def fake_send_file(client, to, text, filename, caption, fallback, reply_to=""):
        out.append(("file", filename, reply_to))

    async def fake_download(client, media_id):
        # The first voice note is the slowest, to prove replies keep their order.
        await asyncio.sleep({"slow": 0.15, "fast": 0.0}.get(media_id, 0.0))
        return media_id.encode(), "audio/ogg"

    async def fake_transcribe(client, audio, mime):
        text = {"slow": "first words", "fast": "second words", "long": "word " * 2000}.get(audio.decode(), "")
        return text, "eng", 0.99

    async def fake_speak(client, text):
        return b"OggS:" + text.encode()

    for name, fake in [("mark_read", fake_read), ("send", fake_send), ("send_voice", fake_send_voice),
                       ("send_file", fake_send_file), ("download", fake_download),
                       ("transcribe_detail", fake_transcribe), ("speak", fake_speak)]:
        monkeypatch.setattr(main, name, fake)
    return main, out


def voice(msg_id, media):
    return {"from": SENDER, "id": msg_id, "type": "audio", "audio": {"id": media}}


def text(msg_id, body):
    return {"from": SENDER, "id": msg_id, "type": "text", "text": {"body": body}}


def run_all(main, messages):
    async def go():
        for m in messages:
            main.dispatch(m)
        while main._running:
            await asyncio.gather(*list(main._running))
    asyncio.run(go())


def test_voice_and_text_replies_in_send_order(wired):
    main, out = wired
    run_all(main, [voice("v1", "slow"), voice("v2", "fast"), text("t3", "hello")])
    assert out == [
        ("text", "first words", "v1"),
        ("text", "second words", "v2"),
        ("voice", b"OggS:hello", "t3"),
    ]
    assert main._previous == {}


def test_long_transcript_comes_as_one_file(wired):
    main, out = wired
    run_all(main, [voice("v1", "long")])
    assert len(out) == 1 and out[0][0] == "file" and out[0][1].startswith("transcript-") and out[0][2] == "v1"


def test_long_text_becomes_several_voice_notes(wired):
    main, out = wired
    run_all(main, [text("t1", ("word " * 1200).strip())])
    assert [kind for kind, _, _ in out] == ["voice", "voice", "voice"]
    assert all(reply == "t1" for _, _, reply in out)


def test_other_messages_get_help(wired):
    main, out = wired
    run_all(main, [{"from": SENDER, "id": "i1", "type": "image", "image": {"id": "x"}}])
    assert out == [("text", main.HELP, "i1")]


def test_arabic_text_gets_translation_then_voice(wired, monkeypatch):
    main, out = wired

    async def fake_plan(text):
        return main.translate.Plan(speak="الحنفية تسرب", show="الحنفية تسرب")
    monkeypatch.setattr(main.translate, "plan_outgoing", fake_plan)
    run_all(main, [text("t1", "Arabic: the tap is leaking")])
    assert out == [("text", "الحنفية تسرب", "t1"), ("voice", "OggS:الحنفية تسرب".encode(), "t1")]


def test_foreign_voice_gets_english_and_persian(wired, monkeypatch):
    main, out = wired

    async def fake_detail(client, audio, mime):
        return "الحنفية تسرب", "ara", 0.97

    async def fake_explain(text):
        return text + "\n\nEN\n\nFA"
    monkeypatch.setattr(main, "transcribe_detail", fake_detail)
    monkeypatch.setattr(main.translate, "needs_translation", lambda lang, p: lang == "ara")
    monkeypatch.setattr(main.translate, "explain_incoming", fake_explain)
    run_all(main, [voice("v1", "fast")])
    assert out == [("text", "الحنفية تسرب\n\nEN\n\nFA", "v1")]
