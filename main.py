"""WhatsApp voice note to text, and text to voice note.

Send or forward a voice note to the bot's WhatsApp number: the bot replies to
it with the text (ElevenLabs Scribe). Send text: the bot replies to it with a
voice note (ElevenLabs Eleven v3, any language). Several at once get their
replies one by one, in the order they were sent. Only numbers in
ALLOWED_WA_NUMBERS get an answer; everyone else is ignored. An empty list
means nobody gets an answer.

A transcript too long for one WhatsApp message comes back as one .txt file;
long text becomes several voice notes. Translation (translate.py): a voice note
in another language gets English and Persian under it; "Arabic: ..." text comes
back as Arabic text plus an Arabic voice note.
"""

import asyncio
import hashlib
import hmac
import logging
import os
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

import translate

GRAPH = "https://graph.facebook.com/v25.0"
ELEVEN_STT = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVEN_TTS = "https://api.elevenlabs.io/v1/text-to-speech"
# One WhatsApp text message holds 4,096 characters. Leave room for "(1/3)".
CHUNK = 3900
# A 25-minute voice note takes ElevenLabs over 2 minutes; wait up to 15.
STT_TIMEOUT = httpx.Timeout(900, connect=15)
# Time zone for file names, as hours from UTC (for example 4 or -5).
LOCAL_TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "0") or 0)))

WA_TOKEN = os.environ.get("WA_TOKEN", "")
WA_PHONE_ID = os.environ.get("WA_PHONE_ID", "")
WA_APP_SECRET = os.environ.get("WA_APP_SECRET", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "")
ELEVEN_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
STT_MODEL = os.environ.get("STT_MODEL", "scribe_v2")
# Empty means auto-detect (Persian and English both work).
STT_LANGUAGE = os.environ.get("STT_LANGUAGE", "")
# Text to speech: Eleven v3 is the only ElevenLabs model that reads Persian,
# and it picks up the language of the text by itself.
TTS_MODEL = os.environ.get("TTS_MODEL", "eleven_v3")
TTS_VOICE = os.environ.get("TTS_VOICE", "JBFqnCBsd6RMkjVDRZzb")
# Characters per voice note; longer text becomes several voice notes in order.
TTS_CHUNK = 2500


def parse_numbers(raw: str) -> set[str]:
    """Comma separated phone numbers in international format, "+" and spaces optional."""
    return {n.strip().lstrip("+").replace(" ", "") for n in raw.split(",") if n.strip()}


ALLOWED = parse_numbers(os.environ.get("ALLOWED_WA_NUMBERS", ""))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voicebot")
if not ALLOWED:
    log.warning("ALLOWED_WA_NUMBERS is empty: the WhatsApp bot will answer nobody")


def allowed_number(sender: str) -> bool:
    """Default deny: only numbers listed in ALLOWED_WA_NUMBERS."""
    return bool(sender) and sender in ALLOWED

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# Meta retries a webhook it thinks failed. Remember recent ids so a voice
# note is never transcribed (and paid for) twice.
_seen: "OrderedDict[str, None]" = OrderedDict()


def seen_before(msg_id: str) -> bool:
    if msg_id in _seen:
        return True
    _seen[msg_id] = None
    if len(_seen) > 500:
        _seen.popitem(last=False)
    return False


def _cut(text: str, size: int) -> list[str]:
    """Split on line or word breaks, never mid-word when it can be helped."""
    parts = []
    rest = text.strip()
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut < size // 2:
            cut = rest.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def _number(parts: list[str]) -> list[str]:
    if len(parts) > 1:
        return [f"({i}/{len(parts)})\n{p}" for i, p in enumerate(parts, 1)]
    return parts


def split_text(text: str, size: int = CHUNK) -> list[str]:
    return _number(_cut(text, size))


def txt_bytes(text: str) -> bytes:
    # The BOM makes iPhone and WhatsApp previews read Persian as UTF-8.
    return ("\ufeff" + text.strip() + "\n").encode("utf-8")


def txt_name(kind: str) -> str:
    return f"{kind}-{datetime.now(LOCAL_TZ):%Y-%m-%d-%H%M}.txt"


HELP = "Send a voice note and I send back the text. Send text and I send back a voice note."

# Per sender: the task answering the previous message. Each reply waits for it,
# so answers come back in the order the messages were sent, while the slow work
# (download, ElevenLabs) still runs side by side.
_previous: dict[str, asyncio.Task] = {}
_running: set[asyncio.Task] = set()


def valid_signature(body: bytes, header: str) -> bool:
    if not WA_APP_SECRET:
        return True
    expected = "sha256=" + hmac.new(WA_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and VERIFY_TOKEN and q.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def incoming(request: Request):
    body = await request.body()
    if not valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        log.warning("bad signature, dropped")
        raise HTTPException(status_code=403)
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                sender = msg.get("from", "")
                if not allowed_number(sender):
                    log.info("ignored message from a number not on the list")
                    continue
                if seen_before(msg.get("id", "")):
                    continue
                dispatch(msg)
    # Answer Meta fast; the work runs after the response.
    return {"ok": True}


def dispatch(msg: dict) -> None:
    # Started in arrival order, so each task finds the one before it.
    task = asyncio.create_task(handle(msg))
    _running.add(task)
    task.add_done_callback(_running.discard)


async def handle(msg: dict) -> None:
    sender = msg["from"]
    me = asyncio.current_task()
    prev = _previous.get(sender)
    _previous[sender] = me
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await mark_read(client, msg["id"])
            answer = await prepare(client, msg)
            if prev:
                await asyncio.wait([prev])
            await answer()
    finally:
        if _previous.get(sender) is me:
            del _previous[sender]


async def prepare(client: httpx.AsyncClient, msg: dict):
    """Do the slow work now; return the reply to send once it is this message's turn."""
    sender, msg_id, kind = msg["from"], msg["id"], msg.get("type")
    if kind == "text":
        text = msg.get("text", {}).get("body", "").strip()
        if text:
            return await prepare_speech(client, sender, msg_id, text)
    media = msg.get(kind) if kind in ("audio", "video", "document") else None
    if kind == "document" and not (media or {}).get("mime_type", "").startswith("audio/"):
        media = None
    if not media:
        return lambda: send(client, sender, HELP, msg_id)
    try:
        audio, mime = await download(client, media["id"])
        text, language, probability = await transcribe_detail(client, audio, mime)
    except Exception:
        log.exception("transcription failed")
        return lambda: send(client, sender, "Could not read this voice note. Please forward it again.", msg_id)
    if not text.strip():
        return lambda: send(client, sender, "No speech found in this voice note.", msg_id)
    log.info("transcribed %d chars", len(text))
    if translate.needs_translation(language, probability):
        text = await translate.explain_incoming(text)
    parts = split_text(text)
    if len(parts) > 1:
        return lambda: send_file(client, sender, text, txt_name("transcript"),
                                 "Too long for one message, so here it is as a file.", parts, msg_id)
    return lambda: send(client, sender, parts[0], msg_id)


async def prepare_speech(client: httpx.AsyncClient, sender: str, msg_id: str, text: str):
    plan = await translate.plan_outgoing(text)
    if plan.ask:
        return lambda: send(client, sender, plan.ask, msg_id)
    try:
        voices = [await speak(client, part) for part in speech_parts(plan.speak)]
    except Exception:
        log.exception("text to speech failed")
        return lambda: send(client, sender, "Could not make the voice note. Please try again.", msg_id)

    async def answer():
        # Translated text first, clean, so it can be forwarded as it is.
        for part in split_text(plan.show) if plan.show else []:
            await send(client, sender, part, msg_id)
        for audio in voices:
            await send_voice(client, sender, audio, msg_id)
    return answer


def speech_parts(text: str) -> list[str]:
    return _cut(text, TTS_CHUNK)


async def speak(client: httpx.AsyncClient, text: str) -> bytes:
    """Text to an Ogg Opus voice note, the format WhatsApp and Telegram voice notes use."""
    r = await client.post(
        f"{ELEVEN_TTS}/{TTS_VOICE}",
        params={"output_format": "opus_48000_32"},
        headers={"xi-api-key": ELEVEN_KEY},
        json={"text": text, "model_id": TTS_MODEL},
        timeout=STT_TIMEOUT,
    )
    if r.status_code >= 400:
        log.error("ElevenLabs TTS %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()
    log.info("spoke %d chars", len(text))
    return r.content


async def send_voice(client: httpx.AsyncClient, to: str, audio: bytes, reply_to: str = "") -> None:
    auth = {"Authorization": f"Bearer {WA_TOKEN}"}
    up = await client.post(
        f"{GRAPH}/{WA_PHONE_ID}/media",
        headers=auth,
        data={"messaging_product": "whatsapp", "type": "audio/ogg"},
        files={"file": ("voice.ogg", audio, "audio/ogg")},
    )
    if up.status_code >= 400:
        log.error("WhatsApp audio upload %s: %s", up.status_code, up.text[:300])
        await send(client, to, "Could not send the voice note. Please try again.", reply_to)
        return
    media_id = up.json()["id"]
    # Try as a voice note first; if this API version rejects the flag, plain audio still plays.
    for audio_obj in ({"id": media_id, "voice": True}, {"id": media_id}):
        payload = {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": audio_obj}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        r = await client.post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers=auth, json=payload)
        if r.status_code < 400:
            return
        log.error("WhatsApp audio %s: %s", r.status_code, r.text[:300])
    await send(client, to, "Could not send the voice note. Please try again.", reply_to)


async def send_file(client: httpx.AsyncClient, to: str, text: str, filename: str,
                    caption: str, fallback: list[str], reply_to: str = "") -> None:
    """Upload text as a .txt and send it as one document. If that fails, send the parts."""
    auth = {"Authorization": f"Bearer {WA_TOKEN}"}
    up = await client.post(
        f"{GRAPH}/{WA_PHONE_ID}/media",
        headers=auth,
        data={"messaging_product": "whatsapp", "type": "text/plain"},
        files={"file": (filename, txt_bytes(text), "text/plain")},
    )
    if up.status_code < 400:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": {"id": up.json()["id"], "filename": filename, "caption": caption},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        r = await client.post(f"{GRAPH}/{WA_PHONE_ID}/messages", headers=auth, json=payload)
        if r.status_code < 400:
            return
        log.error("WhatsApp document %s: %s", r.status_code, r.text[:300])
    else:
        log.error("WhatsApp upload %s: %s", up.status_code, up.text[:300])
    for part in fallback:
        await send(client, to, part, reply_to)


async def mark_read(client: httpx.AsyncClient, msg_id: str, typing: bool = True) -> None:
    # Blue ticks, plus the typing dots while ElevenLabs works.
    payload = {"messaging_product": "whatsapp", "status": "read", "message_id": msg_id}
    if typing:
        payload["typing_indicator"] = {"type": "text"}
    try:
        await client.post(
            f"{GRAPH}/{WA_PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WA_TOKEN}"},
            json=payload,
        )
    except httpx.HTTPError:
        pass


async def download(client: httpx.AsyncClient, media_id: str) -> tuple[bytes, str]:
    auth = {"Authorization": f"Bearer {WA_TOKEN}"}
    meta = await client.get(f"{GRAPH}/{media_id}", headers=auth)
    meta.raise_for_status()
    info = meta.json()
    file = await client.get(info["url"], headers=auth)
    file.raise_for_status()
    return file.content, info.get("mime_type", "audio/ogg")


async def transcribe(client: httpx.AsyncClient, audio: bytes, mime: str) -> str:
    return (await transcribe_detail(client, audio, mime))[0]


async def transcribe_detail(client: httpx.AsyncClient, audio: bytes, mime: str) -> tuple[str, str, float]:
    """Text, the language Scribe heard (ISO code) and how sure it is."""
    form = {"model_id": STT_MODEL, "tag_audio_events": "false"}
    if STT_LANGUAGE:
        form["language_code"] = STT_LANGUAGE
    ext = mime.split("/")[-1].split(";")[0] or "ogg"
    r = await client.post(
        ELEVEN_STT,
        headers={"xi-api-key": ELEVEN_KEY},
        data=form,
        files={"file": (f"voice.{ext}", audio, mime)},
        timeout=STT_TIMEOUT,
    )
    if r.status_code >= 400:
        log.error("ElevenLabs %s: %s", r.status_code, r.text[:300])
    r.raise_for_status()
    data = r.json()
    # Audio length from the last word's end time, logged to track credits per minute.
    words = [w for w in data.get("words") or [] if w.get("end") is not None]
    if words:
        log.info("audio %.0f s, %d bytes", words[-1]["end"], len(audio))
    return data.get("text", ""), data.get("language_code") or "", float(data.get("language_probability") or 0)


async def send(client: httpx.AsyncClient, to: str, text: str, reply_to: str = "") -> None:
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    r = await client.post(
        f"{GRAPH}/{WA_PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json=payload,
    )
    if r.status_code >= 400:
        log.error("WhatsApp send %s: %s", r.status_code, r.text[:300])
