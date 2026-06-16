"""
RadioBulletin mirror bot (polling mode).

Each run:
  1. Logs in with a Telegram USER account (StringSession) -- needed because a
     normal bot cannot read a channel it is not admin of, and your English
     channel's 50 admin slots are full.
  2. Reads every post newer than the last one it processed.
  3. Translates the text into friendly, colloquial Persian (GitHub Models AI,
     with an automatic fallback to Google translate if the AI quota is hit).
  4. Re-posts the FULL content to the Farsi channel: ALL photos/videos kept,
     albums kept as albums, plus a fixed footer with the Farsi channel id.
  5. Saves the new "last processed id" so nothing is duplicated or lost.

Designed to run on GitHub Actions (cron) OR continuously on a small server.
IMPORTANT: use a SECONDARY Telegram account for the session, not your main one.
"""

import os
import re
import json
import asyncio
import logging
import tempfile
import pathlib
import urllib.request
import urllib.error

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

import watermark

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TG_SESSION = os.environ["TG_SESSION"]               # StringSession (make with login.py)
SOURCE_CHANNEL = os.environ["SOURCE_CHANNEL"]       # English: @user or -100xxxx
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"]       # Farsi:   @user or -100xxxx

# The Farsi channel id + signature that must appear at the END of every post.
FOOTER = os.environ.get("FOOTER", "@RadioBulletin | رادیو بولتن")
TARGET_HANDLE = os.environ.get("TARGET_HANDLE", "@RadioBulletin")
# The English channel's @username (without @). Used to scrub it from the text so
# the English id never leaks into the Farsi channel. Optional but recommended.
SOURCE_USERNAME = os.environ.get("SOURCE_USERNAME", "").lstrip("@").lower()

TRANSLATOR = os.environ.get("TRANSLATOR", "github").lower()   # github | google
GH_TOKEN = os.environ.get("GH_MODELS_TOKEN", "")
MODEL = os.environ.get("MODEL", "openai/gpt-4.1")  # best Persian quality on GitHub Models

STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state/last_id.txt"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("radiobulletin")

SYSTEM_PROMPT = (
    "تو یک مترجم خبری حرفه‌ای برای یک کانال تلگرامی فارسی‌زبان هستی. "
    "متن انگلیسی کاربر را به فارسیِ روان، صمیمی و عامه‌پسند ترجمه کن؛ "
    "لحن دوستانه و خودمانی ولی دقیق و امانت‌دار باشد. "
    "اسم‌ها، اعداد، تاریخ‌ها، هشتگ‌ها و لینک‌ها را دست‌نخورده نگه دار. "
    "هیچ توضیح یا مقدمه‌ای ننویس و فقط متن ترجمه‌شده را برگردان."
)


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #
def _translate_github(text: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://models.github.ai/inference/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


def _translate_google(text: str) -> str:
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target="fa")
    if len(text) <= 4500:
        return tr.translate(text)
    out, buf = [], ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > 4500:
            out.append(tr.translate(buf) if buf.strip() else buf)
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        out.append(tr.translate(buf) if buf.strip() else buf)
    return "\n".join(out)


def translate(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if TRANSLATOR == "github" and GH_TOKEN:
        try:
            return _translate_github(text)
        except urllib.error.HTTPError as e:
            # 429 = rate limited / quota -> fall back to Google so no post is lost
            log.warning("GitHub Models HTTP %s (%s). Falling back to Google.", e.code, e.reason)
        except Exception as e:
            log.warning("GitHub Models failed (%s). Falling back to Google.", e)
    try:
        return _translate_google(text)
    except Exception as e:
        log.error("All translators failed (%s). Posting original text.", e)
        return text


def build_caption(raw_text: str) -> str:
    """Translate, scrub the English channel id, and append the Farsi footer."""
    fa = translate(raw_text)
    if SOURCE_USERNAME:
        # replace any @englishhandle with the Farsi handle
        fa = re.sub(rf"@{re.escape(SOURCE_USERNAME)}\b", TARGET_HANDLE, fa, flags=re.IGNORECASE)
        # remove any t.me/englishhandle links
        fa = re.sub(rf"(https?://)?t\.me/{re.escape(SOURCE_USERNAME)}\b", "", fa, flags=re.IGNORECASE)
    fa = fa.strip()
    return f"{fa}\n\n{FOOTER}" if fa else FOOTER


# --------------------------------------------------------------------------- #
# State (last processed message id)
# --------------------------------------------------------------------------- #
def read_state():
    try:
        return int(STATE_FILE.read_text().strip())
    except Exception:
        return None


def write_state(value: int):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(str(value))


# --------------------------------------------------------------------------- #
# Sending (with FloodWait handling + media re-download fallback)
# --------------------------------------------------------------------------- #
async def _send_file(client, target, file, caption):
    while True:
        try:
            return await client.send_file(target, file, caption=caption)
        except FloodWaitError as e:
            log.warning("FloodWait: sleeping %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)


async def _send_text(client, target, text):
    while True:
        try:
            return await client.send_message(target, text, link_preview=False)
        except FloodWaitError as e:
            log.warning("FloodWait: sleeping %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)


def media_kind(m) -> str:
    """Classify a message's media so we know how to watermark it."""
    if m.photo:
        return "image"
    if getattr(m, "video", None) or getattr(m, "video_note", None) or getattr(m, "gif", None):
        return "video"
    doc = getattr(m, "document", None)
    if doc and getattr(doc, "mime_type", None):
        if doc.mime_type.startswith("image/"):
            return "image"
        if doc.mime_type.startswith("video/"):
            return "video"
    return "other"


async def post_group(client, target, group, caption):
    """Post one post: a single message or an album, keeping ALL media.
    If watermarking is enabled, every photo/video is stamped first."""
    media_msgs = [m for m in group if m.media]
    if not media_msgs:
        await _send_text(client, target, caption)
        return

    # Fast path: no watermark -> re-use media already on Telegram servers.
    if not watermark.available():
        try:
            files = [m.media for m in media_msgs]
            await _send_file(client, target, files if len(files) > 1 else files[0], caption)
            return
        except Exception as e:
            log.warning("Direct media re-send failed (%s); downloading then re-uploading.", e)

    # Download -> (watermark) -> re-upload. Used when watermarking, or as fallback.
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for m in media_msgs:
            p = await m.download_media(file=tmp + "/")
            if not p:
                continue
            if watermark.available():
                p = watermark.apply(p, media_kind(m))
            paths.append(p)
        if not paths:
            await _send_text(client, target, caption)
            return
        await _send_file(client, target, paths if len(paths) > 1 else paths[0], caption)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def run():
    client = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    src = await client.get_entity(SOURCE_CHANNEL)
    tgt = await client.get_entity(TARGET_CHANNEL)
    log.info("Logged in as %s | %s -> %s | translator=%s",
             me.username or me.id, SOURCE_CHANNEL, TARGET_CHANNEL, TRANSLATOR)

    last_id = read_state()

    # First run ever: set a baseline at the newest post and do NOT backfill the
    # whole channel history. Mirroring starts from the next new post.
    if last_id is None:
        newest = await client.get_messages(src, limit=1)
        base = newest[0].id if newest else 0
        write_state(base)
        log.info("First run: baseline set to id=%s (no backfill).", base)
        await client.disconnect()
        return

    # Collect every post newer than last_id, oldest first.
    new_msgs = []
    async for m in client.iter_messages(src, min_id=last_id, reverse=True):
        new_msgs.append(m)

    if not new_msgs:
        log.info("No new posts.")
        await client.disconnect()
        return

    # Group consecutive messages that belong to the same album.
    groups, i = [], 0
    while i < len(new_msgs):
        m = new_msgs[i]
        if m.grouped_id:
            grp = [m]
            j = i + 1
            while j < len(new_msgs) and new_msgs[j].grouped_id == m.grouped_id:
                grp.append(new_msgs[j])
                j += 1
            groups.append(grp)
            i = j
        else:
            groups.append([m])
            i += 1

    log.info("Found %d new post(s).", len(groups))

    for grp in groups:
        # skip empty/service messages
        text = next((x.message for x in grp if x.message), "")
        has_media = any(x.media for x in grp)
        if not text.strip() and not has_media:
            write_state(max(x.id for x in grp))
            continue
        try:
            caption = build_caption(text)
            await post_group(client, tgt, grp, caption)
            # checkpoint AFTER success -> nothing lost, nothing duplicated
            write_state(max(x.id for x in grp))
            log.info("Mirrored post id=%s (%d media).",
                     max(x.id for x in grp), sum(1 for x in grp if x.media))
            await asyncio.sleep(2)
        except Exception as e:
            # stop here; next run retries from the same point
            log.error("Failed on post id=%s: %s -- will retry next run.",
                      max(x.id for x in grp), e)
            break

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
