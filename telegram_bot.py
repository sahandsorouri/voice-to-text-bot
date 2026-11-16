"""Telegram voice note to text, and text to voice note.

Send or forward a voice note (or round video, audio file) to the bot and it
replies with the text, using the same ElevenLabs Scribe call as the WhatsApp
bot. Text too long for one message comes back as one .txt file. Several voice
notes forwarded at once get their replies in the order they were sent.
Send text (any language) and it comes back as a voice note (Eleven v3).
Translation (translate.py): a voice note in another language gets English and
Persian under it; "Arabic: ..." text comes back as Arabic text plus a voice note.
Only Telegram user ids in ALLOWED_USER_IDS get an answer; an empty list means
nobody does. Long polling, so no webhook or open port is needed.
"""

import asyncio
import json
import logging
import os

import httpx

import translate
from main import speak, speech_parts, split_text, transcribe_detail, txt_bytes, txt_name

TG_TOKEN = os.environ.get("TG_TOKEN", "")
API = f"https://api.telegram.org/bot{TG_TOKEN}"
FILES = f"https://api.telegram.org/file/bot{TG_TOKEN}"


def parse_ids(raw: str) -> set[int]:
    """Comma separated numeric Telegram user ids; anything else is skipped."""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


ALLOWED = parse_ids(os.environ.get("ALLOWED_USER_IDS", ""))
# Bot API downloads stop at 20 MB.
MAX_BYTES = 20 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# httpx logs full URLs, and Telegram URLs carry the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tgvoice")
if not ALLOWED:
    log.warning("ALLOWED_USER_IDS is empty: the Telegram bot will answer nobody")


def allowed(msg: dict) -> bool:
    """Default deny: only user ids listed in ALLOWED_USER_IDS."""
    user_id = (msg.get("from") or {}).get("id")
    return isinstance(user_id, int) and user_id in ALLOWED


def find_media(msg: dict) -> dict | None:
    """The voice-like part of a message: voice, round video, audio, video or an audio file."""
    for key, mime in (("voice", "audio/ogg"), ("video_note", "video/mp4"), ("audio", "audio/mpeg"), ("video", "video/mp4")):
        if key in msg:
            return {**msg[key], "mime_type": msg[key].get("mime_type", mime)}
    doc = msg.get("document")
    if doc and doc.get("mime_type", "").startswith(("audio/", "video/")):
        return doc
    return None


async def reply(client: httpx.AsyncClient, msg: dict, text: str) -> None:
    await client.post(f"{API}/sendMessage", json={
        "chat_id": msg["chat"]["id"],
        "text": text,
        "reply_parameters": {"message_id": msg["message_id"], "allow_sending_without_reply": True},
    })


async def reply_voice(client: httpx.AsyncClient, msg: dict, voices: list[bytes]) -> None:
    for audio in voices:
        r = await client.post(f"{API}/sendVoice", data={
            "chat_id": msg["chat"]["id"],
            "reply_parameters": json.dumps({"message_id": msg["message_id"], "allow_sending_without_reply": True}),
        }, files={"voice": ("voice.ogg", audio, "audio/ogg")})
        if r.status_code >= 400:
            log.error("sendVoice %s", r.status_code)
            await reply(client, msg, "Could not send the voice note. Please try again.")
            return


async def reply_file(client: httpx.AsyncClient, msg: dict, text: str, fallback: list[str]) -> None:
    name = txt_name("transcript")
    r = await client.post(f"{API}/sendDocument", data={
        "chat_id": msg["chat"]["id"],
        "caption": "Too long for one message, so here it is as a file.",
        "reply_parameters": json.dumps({"message_id": msg["message_id"], "allow_sending_without_reply": True}),
    }, files={"document": (name, txt_bytes(text), "text/plain")})
    if r.status_code < 400:
        return
    log.error("sendDocument %s", r.status_code)
    for part in fallback:
        await reply(client, msg, part)


# Per chat: the task answering the previous message. Each answer waits for it,
# so replies come back in the order the voice notes were sent, while the
# transcriptions themselves still run side by side.
_previous: dict[int, asyncio.Task] = {}


async def handle(client: httpx.AsyncClient, msg: dict) -> None:
    if not allowed(msg):
        log.info("ignored message from a user not on the list")
        return
    chat = msg["chat"]["id"]
    me = asyncio.current_task()
    prev = _previous.get(chat)
    _previous[chat] = me
    try:
        answer = await prepare(client, msg)
        if prev:
            await asyncio.wait([prev])
        await answer()
    finally:
        if _previous.get(chat) is me:
            del _previous[chat]


async def keep_typing(client: httpx.AsyncClient, chat: int) -> None:
    # Telegram drops "typing..." after 5 s; long voice notes take minutes.
    try:
        while True:
            await client.post(f"{API}/sendChatAction", json={"chat_id": chat, "action": "typing"})
            await asyncio.sleep(4.5)
    except (asyncio.CancelledError, httpx.HTTPError):
        pass


HELP = "Send a voice note and I send back the text. Send text and I send back a voice note."


async def prepare_speech(client: httpx.AsyncClient, msg: dict, text: str):
    await client.post(f"{API}/sendChatAction", json={"chat_id": msg["chat"]["id"], "action": "record_voice"})
    plan = await translate.plan_outgoing(text)
    if plan.ask:
        return lambda: reply(client, msg, plan.ask)
    try:
        voices = [await speak(client, part) for part in speech_parts(plan.speak)]
    except Exception as e:
        log.error("text to speech failed: %s", type(e).__name__)
        return lambda: reply(client, msg, "Could not make the voice note. Please try again.")

    async def answer():
        # Translated text first, clean, so it can be forwarded as it is.
        for part in split_text(plan.show) if plan.show else []:
            await reply(client, msg, part)
        await reply_voice(client, msg, voices)
    return answer


async def prepare(client: httpx.AsyncClient, msg: dict):
    """Do the slow work now; return the reply to send once it is this message's turn."""
    media = find_media(msg)
    text_in = (msg.get("text") or "").strip()
    if not media and text_in and not text_in.startswith("/"):
        return await prepare_speech(client, msg, text_in)
    if not media:
        return lambda: reply(client, msg, HELP)
    if media.get("file_size", 0) > MAX_BYTES:
        return lambda: reply(client, msg, "This file is over 20 MB. Telegram does not let bots download it.")
    typing = asyncio.create_task(keep_typing(client, msg["chat"]["id"]))
    try:
        info = (await client.get(f"{API}/getFile", params={"file_id": media["file_id"]})).json()["result"]
        audio = (await client.get(f"{FILES}/{info['file_path']}")).content
        text, language, probability = await transcribe_detail(client, audio, media["mime_type"])
    except Exception as e:
        log.error("transcription failed: %s", type(e).__name__)
        return lambda: reply(client, msg, "Could not read this voice note. Please send it again.")
    finally:
        typing.cancel()
    if not text.strip():
        return lambda: reply(client, msg, "No speech found in this voice note.")
    log.info("transcribed %d chars", len(text))
    if translate.needs_translation(language, probability):
        text = await translate.explain_incoming(text)
    parts = split_text(text)
    if len(parts) > 1:
        return lambda: reply_file(client, msg, text, parts)
    return lambda: reply(client, msg, parts[0])


async def main() -> None:
    offset = 0
    async with httpx.AsyncClient(timeout=120) as client:
        # Nothing to type: hide the / command menu.
        await client.post(f"{API}/deleteMyCommands")
        log.info("polling, %d allowed users", len(ALLOWED))
        while True:
            try:
                r = await client.get(f"{API}/getUpdates", params={
                    "offset": offset, "timeout": 50, "allowed_updates": '["message"]',
                }, timeout=70)
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    if "message" in upd:
                        asyncio.create_task(handle(client, upd["message"]))
            except Exception as e:
                log.warning("poll error: %s", type(e).__name__)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
