import importlib


USER = 111


def load(monkeypatch, ids="111, 222"):
    monkeypatch.setenv("ALLOWED_USER_IDS", ids)
    import telegram_bot
    importlib.reload(telegram_bot)
    return telegram_bot


def test_allowed_by_user_id(monkeypatch):
    tg = load(monkeypatch)
    assert tg.allowed({"from": {"id": 111}})
    assert tg.allowed({"from": {"id": 222, "username": "anyone"}})
    assert not tg.allowed({"from": {"id": 333}})
    assert not tg.allowed({"from": {"username": "someone"}})
    assert not tg.allowed({"from": {}})
    assert not tg.allowed({})


def test_empty_allowlist_answers_nobody(monkeypatch):
    tg = load(monkeypatch, ids="")
    assert tg.ALLOWED == set()
    assert not tg.allowed({"from": {"id": 111}})
    assert tg.parse_ids("12, x, ,34") == {12, 34}


def test_find_media(monkeypatch):
    tg = load(monkeypatch)
    assert tg.find_media({"voice": {"file_id": "v"}})["mime_type"] == "audio/ogg"
    assert tg.find_media({"video_note": {"file_id": "r"}})["mime_type"] == "video/mp4"
    assert tg.find_media({"document": {"file_id": "d", "mime_type": "audio/mp4"}})["file_id"] == "d"
    assert tg.find_media({"document": {"file_id": "d", "mime_type": "application/pdf"}}) is None
    assert tg.find_media({"text": "hi"}) is None


def test_replies_keep_send_order(monkeypatch):
    import asyncio
    tg = load(monkeypatch)
    sent = []
    # The first voice note is the slowest to transcribe.
    delays = {1: 0.15, 2: 0.0, 3: 0.05}

    async def fake_prepare(client, msg):
        await asyncio.sleep(delays[msg["message_id"]])

        async def answer():
            sent.append(msg["message_id"])
        return answer

    monkeypatch.setattr(tg, "prepare", fake_prepare)

    def voice(i):
        return {"message_id": i, "chat": {"id": 7}, "from": {"id": USER}, "voice": {"file_id": f"f{i}"}}

    async def run():
        tasks = [asyncio.create_task(tg.handle(None, voice(i))) for i in (1, 2, 3)]
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert sent == [1, 2, 3]
    assert tg._previous == {}


def test_text_becomes_voice_and_commands_get_help(monkeypatch):
    import asyncio
    tg = load(monkeypatch)
    calls = []

    class FakeClient:
        async def post(self, url, **kw):
            calls.append(url.rsplit("/", 1)[-1])

            class R:
                status_code = 200
            return R()

    async def fake_speak(client, text):
        return b"OggS" + text.encode()

    monkeypatch.setattr(tg, "speak", fake_speak)
    msg = {"message_id": 1, "chat": {"id": 7}, "from": {"id": USER}, "text": "hello world"}
    answer = asyncio.run(tg.prepare(FakeClient(), msg))
    asyncio.run(answer())
    assert calls == ["sendChatAction", "sendVoice"]

    calls.clear()
    answer = asyncio.run(tg.prepare(FakeClient(), {**msg, "text": "/start"}))
    asyncio.run(answer())
    assert calls == ["sendMessage"]
