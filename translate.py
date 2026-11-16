"""Optional translation for both bots, through the Anthropic API.

Incoming voice in a language other than Persian or English: the reply shows the
original text, then English, then Persian.

Outgoing text that starts with a language name and a colon or a line break
("Arabic: the tap is leaking"): the reply is the translated text plus a voice
note in that language. The name may be in English or Persian, and misspelled;
the model reads it. Without both ANTHROPIC_API_KEY and TRANSLATE_MODEL set,
nothing is translated and both bots only do voice to text and text to voice.
"""

import json
import logging
import os
import re
from dataclasses import dataclass

import anthropic

MODEL = os.environ.get("TRANSLATE_MODEL", "")
log = logging.getLogger("voicebot")

# Scribe language codes the reader understands already (Persian incl. Dari, English).
NATIVE = {"fa", "fas", "per", "pes", "prs", "en", "eng"}
# The language name: at most 3 words, then a colon or a line break, then the message.
HEAD = re.compile(r"^\s*([^:：\n]{1,30}?)\s*(?:[:：]|\n)\s*(\S[\s\S]*)$")

_client = None


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and bool(MODEL)


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(timeout=60, max_retries=2)
    return _client


class TranslateError(Exception):
    pass


async def _ask(system: str, user: str, schema: dict) -> dict:
    response = await client().messages.create(
        model=MODEL,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    )
    if response.stop_reason == "refusal":
        raise TranslateError(f"refused: {response.stop_details.category if response.stop_details else None}")
    if response.stop_reason == "max_tokens":
        raise TranslateError("too long")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return json.loads(text)


# MARK: incoming voice

def needs_translation(language: str, probability: float) -> bool:
    return enabled() and bool(language) and language.lower() not in NATIVE and probability >= 0.5


INCOMING_SYSTEM = (
    "You translate a transcribed voice message so the reader understands it. "
    "Give a plain, natural English translation and a plain, natural Persian translation. "
    "Keep names, numbers, times, prices and addresses exactly as they are. "
    "Translate only; add no notes or explanations."
)
INCOMING_SCHEMA = {
    "type": "object",
    "properties": {"english": {"type": "string"}, "persian": {"type": "string"}},
    "required": ["english", "persian"],
    "additionalProperties": False,
}


async def explain_incoming(text: str) -> str:
    """Original text, then English, then Persian. Each label on its own line,
    so a Persian body below it stays right-to-left in WhatsApp."""
    try:
        out = await _ask(INCOMING_SYSTEM, f"<message>\n{text}\n</message>", INCOMING_SCHEMA)
    except (anthropic.APIError, TranslateError, ValueError) as e:
        log.error("incoming translation failed: %s", type(e).__name__)
        return f"{text}\n\n(Could not translate this one.)"
    log.info("translated incoming %d chars", len(text))
    return f"{text}\n\n🇬🇧 English\n{out['english'].strip()}\n\n🇮🇷 Persian\n{out['persian'].strip()}"


# MARK: outgoing text

@dataclass
class Plan:
    speak: str = ""        # what the voice note says
    show: str = ""         # translated text to send first; empty when not translating
    ask: str = ""          # a short question instead of a voice note


def split_target(text: str) -> tuple[str, str] | None:
    m = HEAD.match(text)
    if not m:
        return None
    head, body = m.group(1).strip(), m.group(2).strip()
    if not body or len(head.split()) > 3:
        return None
    return head, body


OUTGOING_SYSTEM = (
    "The user writes a message and puts a target language name before it. "
    "The name may be in English or Persian, in any script, and may be misspelled. "
    "Decide which language it names. "
    "If it clearly names one language: status 'translate', the language's English name, and the message "
    "translated into it the way a native speaker would text it: short, simple, polite, everyday wording. "
    "Keep names, numbers, times, prices and addresses exactly. Output only the translation, no notes. "
    "If the name is not a language at all (it is simply the first words of the message): status 'not_language'. "
    "If it could mean more than one language and you cannot tell which: status 'unsure'. "
    "For 'not_language' and 'unsure' leave language and translation empty."
)
OUTGOING_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["translate", "not_language", "unsure"]},
        "language": {"type": "string"},
        "translation": {"type": "string"},
    },
    "required": ["status", "language", "translation"],
    "additionalProperties": False,
}


async def plan_outgoing(text: str) -> Plan:
    """No language name in front (or no key): speak the text as it is, like before."""
    target = split_target(text) if enabled() else None
    if not target:
        return Plan(speak=text)
    head, body = target
    try:
        out = await _ask(OUTGOING_SYSTEM, f"<language>{head}</language>\n<message>\n{body}\n</message>", OUTGOING_SCHEMA)
    except (anthropic.APIError, TranslateError, ValueError) as e:
        log.error("outgoing translation failed: %s", type(e).__name__)
        return Plan(ask="Could not translate this one. Please send it again.")
    if out["status"] == "translate" and out["translation"].strip():
        log.info("translated outgoing to %s", out["language"])
        translation = out["translation"].strip()
        return Plan(speak=translation, show=translation)
    if out["status"] == "unsure":
        return Plan(ask=f'Which language did you mean by "{head}"? Send it again with the full name, for example: Bengali: {body[:40]}')
    return Plan(speak=text)
