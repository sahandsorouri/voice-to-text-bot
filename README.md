# Voice to Text Bot

A small bot for Telegram and WhatsApp that turns voice notes into text, and text into voice notes, with ElevenLabs.

- Send or forward a voice note (or a round video, audio file or video): the bot replies with the text, using ElevenLabs Scribe.
- Send text in any language: the bot replies with a voice note, using ElevenLabs Eleven v3.
- Forward several at once: replies come back in the order they were sent, while the work runs side by side.
- Text too long for one message comes back as one `.txt` file. Long text becomes several voice notes.
- Optional translation (needs an Anthropic API key): a voice note in a language other than Persian or English gets an English and a Persian translation under it. Text that starts with a language name and a colon, like `Arabic: the tap is leaking`, comes back as the translated text plus a voice note in that language.

Two programs share one Docker image:

- `main.py`: the WhatsApp bot, a FastAPI webhook for the WhatsApp Cloud API.
- `telegram_bot.py`: the Telegram bot, long polling, no open port needed.

## Setup

1. Create a Telegram bot with BotFather and copy its token.
2. For WhatsApp: create a Meta app with the WhatsApp product, get a permanent access token and the phone number id, and point the webhook at `https://<your-domain>/webhook` with your verify string. Subscribe to the `messages` field.
3. Get an ElevenLabs API key.
4. Copy `.env.example` to `.env` and fill it in. Add your own Telegram user id and WhatsApp number to the allowlists.
5. Run:

```
docker compose up -d --build
```

The WhatsApp webhook listens on `127.0.0.1:8000` on the host. Put a TLS reverse proxy (nginx, Caddy) in front of it, since Meta only calls HTTPS URLs.

To run without Docker:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --port 8000   # WhatsApp
.venv/bin/python telegram_bot.py          # Telegram
```

## Environment variables

| Name | Required | What it is |
|---|---|---|
| `ELEVENLABS_API_KEY` | yes | ElevenLabs API key |
| `STT_MODEL` | no | Speech to text model, default `scribe_v2` |
| `STT_LANGUAGE` | no | Force a language code (for example `fas`); empty means auto-detect |
| `TTS_MODEL` | no | Text to speech model, default `eleven_v3` |
| `TTS_VOICE` | no | ElevenLabs voice id; a default voice is built in |
| `TG_TOKEN` | for Telegram | Telegram bot token |
| `ALLOWED_USER_IDS` | for Telegram | Comma separated numeric Telegram user ids allowed to use the bot |
| `WA_TOKEN` | for WhatsApp | WhatsApp Cloud API access token |
| `WA_PHONE_ID` | for WhatsApp | WhatsApp phone number id |
| `WA_APP_SECRET` | for WhatsApp | Meta app secret, used to check webhook signatures |
| `VERIFY_TOKEN` | for WhatsApp | Any random string, also set in Meta's webhook settings |
| `ALLOWED_WA_NUMBERS` | for WhatsApp | Comma separated phone numbers in international format, without `+` |
| `ANTHROPIC_API_KEY` | no | Turns on translation, together with `TRANSLATE_MODEL` |
| `TRANSLATE_MODEL` | no | Anthropic model id used for translation |
| `TZ_OFFSET_HOURS` | no | Hours from UTC for transcript file names, default `0` |
| `HOST_PORT` | no | Host port for the WhatsApp webhook, default `8000` |

## Access

Both bots are default deny.

- Telegram: only user ids in `ALLOWED_USER_IDS` get an answer. You can find your id by messaging a bot such as `userinfobot`.
- WhatsApp: only numbers in `ALLOWED_WA_NUMBERS` get an answer.

If a list is empty, that bot answers nobody and logs a warning at start. Everyone else is ignored silently. Every voice note costs ElevenLabs credits, so keep the lists short.

## Tests

```
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q tests
```

The tests run offline and never call a real API.

## License

MIT, see `LICENSE`.

Built by Sahand Sorouri, https://sahand.sorouri.com
