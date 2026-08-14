"""PoriotCloud Vault Telegram bot.

Flow: user sends a .dark file (or pastes a darktunnel:// link)
  → animated unicode progress bar (message edited ~5x)
  → summary card
  → the signed .json file  AND  the PoriotCloud Vault link (bot = both)

Two run modes:
  python bot.py                     # standalone (Termux / local)
  BOT_TOKEN set + uvicorn server    # co-hosted inside the FastAPI process
"""
import io
import json
import os
import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import decoder

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TEXT_LEN = 512 * 1024
CREDIT = "@Poriot_ke · PoriotCloud Vault"

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def vault_api_url() -> str:
    """Where to POST decoded configs. Falls back to the public site URL so
    the co-hosted bot (same Railway service) uses its own API. A bare domain
    is normalized to https:// so vault links are always secure."""
    base = (env("VAULT_API_URL") or env("VAULT_PUBLIC_URL") or "").strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


def vault_api_token() -> str:
    return env("VAULT_API_TOKEN")


def load_token() -> str:
    token = env("BOT_TOKEN", "").strip()
    if token:
        return token
    if os.path.exists("token.txt"):
        token = open("token.txt", encoding="utf-8").read().strip()
        if token:
            return token
    raise SystemExit(
        "No bot token. Set BOT_TOKEN or create token.txt:\n"
        "    echo '123456:ABC...' > token.txt\n"
    )


# --------------------------------------------------------------------------
# Progress bar (unicode, edited message ~5x — what Telegram can render)
# --------------------------------------------------------------------------

STAGES = [
    (8,  "⬇️ Downloading file…",      "from Telegram · in memory"),
    (34, "🔐 Decrypting layer 1/2",   "AES-256-CFB · 32-byte key"),
    (62, "🔐 Decrypting layer 2/2",   "MessagePack · unpacking"),
    (87, "🧹 Cleaning & signing…",    "credit @Poriot_ke"),
    (100, "✅ Done!",                 "summary + vault link ↓"),
]


def bar(pct: int, width: int = 10) -> str:
    full = round(pct / 100 * width)
    return "▰" * full + "▱" * (width - full)


async def _edit_progress(message, pct: int, label: str, sub: str, fname: str):
    text = (
        f"🔓 Decoding `{fname}`…\n"
        f"{bar(pct)} {pct:3d}%\n"
        f"{label}\n"
        f"`{sub}`"
    )
    await message.edit_text(text, parse_mode="Markdown")


# --------------------------------------------------------------------------
# Vault upload
# --------------------------------------------------------------------------

async def upload_to_vault(config: dict, fname: str) -> str | None:
    """POST the signed config to the vault server. Returns the vault URL."""
    base = vault_api_url()
    token = vault_api_token()
    if not base or not token:
        return None
    try:
        import urllib.request

        signed = decoder.sign_result(config, by=CREDIT)
        body = json.dumps({"name": fname, "config": signed}).encode("utf-8")
        req = urllib.request.Request(
            base + "/api/vault",
            data=body,
            headers={"Content-Type": "application/json", "X-Vault-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("url")
    except Exception:
        return None


# --------------------------------------------------------------------------
# Decode + reply
# --------------------------------------------------------------------------

async def _decode_and_reply(update: Update, payload, status, fname: str):
    msg = update.effective_message
    try:
        # stage 1: downloading (already in memory for pasted links — cosmetic)
        await _edit_progress(status, *STAGES[0], fname)
        result = decoder.decode_darktunnel(payload)
        signed = decoder.sign_result(result, by=CREDIT)
        await _edit_progress(status, *STAGES[1], fname)
        await _edit_progress(status, *STAGES[2], fname)
        await _edit_progress(status, *STAGES[3], fname)
    except decoder.DecodeError as exc:
        await status.edit_text(f"❌ {exc}")
        return
    except Exception as exc:
        await status.edit_text(f"❌ Unexpected error: {exc}")
        return

    # stage 4: done — the vault link is the ONLY delivery channel.
    # The file lives on the vault page (where the ad slots are).
    await _edit_progress(status, *STAGES[4], fname)

    summary = decoder.summarize(signed)
    await msg.reply_text(summary)

    url = await upload_to_vault(signed, fname)
    if url:
        await msg.reply_text(
            f"🔗 <b>Your decrypted config</b>\n{url}\n\n"
            "📄 JSON viewer + copy + download on the page\n"
            "⏳ Auto-destroys in <b>6 hours</b>\n"
            "⚡ Signed by @Poriot_ke",
            parse_mode="HTML",
        )
    else:
        # Last-resort fallback: vault unreachable → send the file so the user
        # isn't left empty-handed. (Rare — vault link is the primary delivery.)
        buffer = io.BytesIO(
            json.dumps(signed, indent=2, ensure_ascii=False).encode("utf-8")
        )
        await msg.reply_document(
            document=buffer,
            filename=decoder.suggest_filename(signed),
            caption="ℹ️ Vault unavailable right now — signed config attached. "
                    "Re-send in a bit for a vault link.",
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.effective_message.document
    if doc.file_size and doc.file_size > MAX_FILE_BYTES:
        await update.effective_message.reply_text(
            f"❌ File too big ({doc.file_size // (1024*1024)} MB). Max 8 MB."
        )
        return

    status = await update.effective_message.reply_text(
        "🔓 Decoding…", reply_to_message_id=update.effective_message.message_id
    )
    try:
        file = await doc.get_file()
        payload = await file.download_as_bytearray()
    except Exception as exc:
        await status.edit_text(f"❌ Couldn't download the file: {exc}")
        return
    await _decode_and_reply(update, payload, status, doc.file_name or "config.dark")


_BASE64ISH = re.compile(r'^[A-Za-z0-9+/=\-_]+$')


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if not text or text.startswith("/"):
        return
    is_uri = "darktunnel://" in text
    is_blob = len(text) > 80 and _BASE64ISH.match(text) is not None
    if not (is_uri or is_blob):
        return
    if len(text) > MAX_TEXT_LEN:
        await update.effective_message.reply_text("❌ Message too long.")
        return
    status = await update.effective_message.reply_text(
        "🔓 Decoding…", reply_to_message_id=update.effective_message.message_id
    )
    await _decode_and_reply(update, text, status, "pasted.dark")


async def cmd_decode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply = update.effective_message.reply_to_message
    if reply is None or reply.document is None:
        await update.effective_message.reply_text(
            "Reply to a <code>.dark</code> file with /decode to decode it.",
            parse_mode="HTML",
        )
        return
    await handle_document(update, context)


HELP_TEXT = (
    "🤖 <b>PoriotCloud Vault Bot</b>\n\n"
    "Send me a <code>.dark</code> config file (or paste a "
    "<code>darktunnel://…</code> link) and get:\n"
    "  • a summary card\n"
    "  • a <b>PoriotCloud Vault link</b> — the decrypted config lives on "
    "that page (JSON viewer + copy + download), auto-destroys after 6h\n\n"
    "Commands:\n"
    "  /start — intro\n  /help — this message\n"
    "  /decode — decode the file you replied to\n\n"
    "ℹ️ Tip: decode in a private chat — configs contain credentials.\n"
    "⚡ Signed by <b>@Poriot_ke</b>"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        HELP_TEXT, parse_mode="HTML",
        reply_to_message_id=update.effective_message.message_id,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="HTML")


# --------------------------------------------------------------------------
# Application building (shared by both run modes)
# --------------------------------------------------------------------------

def build_application(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("decode", cmd_decode))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


async def run_bot_async() -> None:
    """Non-blocking polling for the co-hosted (FastAPI) run mode."""
    token = env("BOT_TOKEN", "").strip()
    if not token:
        return
    application = build_application(token)
    await application.initialize()
    await application.start()
    await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    application = build_application(load_token())
    print("🤖 PoriotCloud Vault bot running… (Ctrl+C to stop)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
