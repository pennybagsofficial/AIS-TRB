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
import time
import html as html_lib
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

# AI model fallback chain: best Persian first, then higher-limit models. If one
# hits its rate limit, the bot switches to the next (all keep HTML formatting).
# Google is the very last resort (plain text only).
MODELS = [m.strip() for m in os.environ.get(
    "MODELS",
    "openai/gpt-4.1,openai/gpt-4o,openai/gpt-4.1-mini,openai/gpt-4o-mini"
).split(",") if m.strip()]
_PRIMARY = os.environ.get("MODEL", "").strip()   # optional override for the first model
if _PRIMARY and _PRIMARY not in MODELS:
    MODELS.insert(0, _PRIMARY)

# When a model is rate-limited we remember it (across runs) so we don't keep
# wasting a doomed request on it for every post.
COOLDOWN_FILE = pathlib.Path(os.environ.get("COOLDOWN_FILE", "state/model_cooldowns.json"))
COOLDOWN_MAX = int(os.environ.get("COOLDOWN_MAX", "21600"))   # cap any cooldown at 6h
COOLDOWN_DEFAULT = int(os.environ.get("COOLDOWN_DEFAULT", "1800"))  # if no retry-after

STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state/last_id.txt"))

# Keep a single run short so it never blocks the queue. Anything left over is
# picked up by the next trigger (state is checkpointed after every post).
RUN_BUDGET = int(os.environ.get("RUN_BUDGET", "240"))   # seconds per run
MAX_FLOOD = int(os.environ.get("MAX_FLOOD", "90"))      # if FloodWait longer, stop & resume next run
POST_DELAY = float(os.environ.get("POST_DELAY", "1"))   # gap between posts

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("radiobulletin")

SYSTEM_PROMPT = (
    "تو یک مترجم خبری حرفه‌ای برای یک کانال تلگرامی فارسی‌زبان هستی. "
    "ورودی یک متن با قالب‌بندی HTML تلگرام است (تگ‌هایی مثل "
    "<b>, <i>, <u>, <s>, <a>, <code>, <pre>, <blockquote>). "
    "آن را به فارسیِ روان، صمیمی و عامه‌پسند ترجمه کن و این قواعد را کامل رعایت کن:\n"
    "۱) همه‌ی تگ‌های HTML را حفظ کن و دقیقاً دور همان بخشِ ترجمه‌شده‌ی متناظر بگذار "
    "(اگر بخشی <b> بود معادل فارسی‌اش هم <b> باشد؛ کوت‌ها داخل <blockquote> بمانند).\n"
    "۲) محتوای داخل <code> و <pre> و مقدار آدرس لینک‌ها (href) را ترجمه نکن و دست نزن.\n"
    "۳) ساختار، خطوط جدید، فاصله‌ها و ترتیب را عیناً مثل منبع نگه دار.\n"
    "۴) اسم‌ها، اعداد، تاریخ‌ها، هشتگ‌ها و یوزرنیم‌ها را حفظ کن.\n"
    "۵) فقط خروجی HTML نهایی را برگردان؛ بدون هیچ توضیح، عنوان یا بک‌تیک."
)


# --------------------------------------------------------------------------- #
# Translation
# --------------------------------------------------------------------------- #
def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    return s


# --- model cooldowns (persisted across runs in state/model_cooldowns.json) --- #
def _load_cooldowns() -> dict:
    try:
        return json.loads(COOLDOWN_FILE.read_text())
    except Exception:
        return {}


def save_cooldowns():
    now = time.time()
    fresh = {k: v for k, v in _COOLDOWNS.items() if v > now}   # prune expired
    try:
        COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_FILE.write_text(json.dumps(fresh, sort_keys=True))
    except Exception as e:
        log.warning("Could not save model cooldowns: %s", e)


_COOLDOWNS = _load_cooldowns()


def _retry_after(e) -> int:
    try:
        return int(e.headers.get("retry-after", "0") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _post_github(model: str, text: str) -> str:
    """One inference call to a single model. Raises on failure."""
    body = json.dumps({
        "model": model,
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
    return _strip_fences(data["choices"][0]["message"]["content"])


def _translate_github_chain(text: str) -> str:
    """Try each model in order, skipping any still on cooldown. Returns the
    translated HTML, or raises RuntimeError if no model is available right now."""
    now = time.time()
    last_err = None
    for model in MODELS:
        if _COOLDOWNS.get(model, 0) > now:
            continue  # still cooling down from an earlier limit -> skip silently
        try:
            return _post_github(model, text)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = _retry_after(e)
                # brief per-minute throttle: wait once (if short) and retry same model
                if 0 < wait <= 20:
                    time.sleep(wait + 1)
                    try:
                        return _post_github(model, text)
                    except Exception as e2:
                        last_err = e2
                cd = min(wait or COOLDOWN_DEFAULT, COOLDOWN_MAX)
                _COOLDOWNS[model] = now + cd
                log.warning("Model %s rate-limited; cooling %ss, switching to next model.", model, cd)
            elif 400 <= e.code < 500:
                _COOLDOWNS[model] = now + COOLDOWN_MAX   # bad/unavailable model id
                log.warning("Model %s returned HTTP %s; disabling temporarily.", model, e.code)
            else:                                        # 5xx: transient
                _COOLDOWNS[model] = now + 60
                log.warning("Model %s returned HTTP %s; brief cooldown.", model, e.code)
        except Exception as e:                           # network / timeout
            last_err = e
            _COOLDOWNS[model] = now + 60
            log.warning("Model %s call failed (%s); brief cooldown.", model, e)
    raise RuntimeError(f"all AI models unavailable ({last_err})")


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


def _scrub_source(s: str) -> str:
    """Replace the English channel id/links with the Farsi ones."""
    if SOURCE_USERNAME:
        s = re.sub(rf"@{re.escape(SOURCE_USERNAME)}\b", TARGET_HANDLE, s, flags=re.IGNORECASE)
        s = re.sub(rf"t\.me/{re.escape(SOURCE_USERNAME)}\b",
                   f"t.me/{TARGET_HANDLE.lstrip('@')}", s, flags=re.IGNORECASE)
    return s


def build_caption(html_text: str, plain_text: str) -> str:
    """Return an HTML caption (used with parse_mode=html): the translated body
    with the SAME formatting as the source (bold/italic/quote/links), followed by
    a BOLD footer that carries the Farsi channel id."""
    bold_footer = f"<b>{html_lib.escape(FOOTER)}</b>"

    # Preferred path: AI model chain (keeps HTML formatting; switches model on limit).
    if TRANSLATOR == "github" and GH_TOKEN and html_text.strip():
        try:
            fa = _scrub_source(_translate_github_chain(html_text)).strip()
            return f"{fa}\n\n{bold_footer}" if fa else bold_footer
        except Exception as e:
            log.warning("All AI models unavailable (%s). Falling back to Google.", e)

    # Last resort: Google (plain text only; formatting can't be preserved here).
    src = (plain_text or "").strip()
    if not src:
        return bold_footer
    try:
        fa = _translate_google(src)
    except Exception as e:
        log.error("All translators failed (%s). Posting original text.", e)
        fa = src
    fa = _scrub_source(html_lib.escape(fa)).strip()   # escape so plain text is valid HTML
    return f"{fa}\n\n{bold_footer}" if fa else bold_footer


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
            if e.seconds > MAX_FLOOD:
                log.warning("FloodWait %ss exceeds cap; stopping run, will resume next trigger.", e.seconds)
                raise
            log.warning("FloodWait: sleeping %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)


async def _send_text(client, target, text):
    while True:
        try:
            return await client.send_message(target, text, link_preview=False)
        except FloodWaitError as e:
            if e.seconds > MAX_FLOOD:
                log.warning("FloodWait %ss exceeds cap; stopping run, will resume next trigger.", e.seconds)
                raise
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
    client.parse_mode = "html"   # so source formatting + bold footer render correctly
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

    start = time.monotonic()
    for grp in groups:
        # stop gracefully if this run is taking too long; next trigger resumes
        if time.monotonic() - start > RUN_BUDGET:
            log.info("Run budget reached; stopping. Remaining posts handled next trigger.")
            break

        # text-bearing message of the group (its HTML keeps bold/italic/quote/links)
        text_msg = next((x for x in grp if x.message), None)
        html_text = text_msg.text if text_msg else ""       # HTML (parse_mode=html)
        plain_text = text_msg.message if text_msg else ""    # plain (Google fallback)
        has_media = any(x.media for x in grp)

        if not plain_text.strip() and not has_media:
            write_state(max(x.id for x in grp))
            continue
        try:
            caption = build_caption(html_text, plain_text)
            await post_group(client, tgt, grp, caption)
            # checkpoint AFTER success -> nothing lost, nothing duplicated
            write_state(max(x.id for x in grp))
            log.info("Mirrored post id=%s (%d media).",
                     max(x.id for x in grp), sum(1 for x in grp if x.media))
            await asyncio.sleep(POST_DELAY)
        except Exception as e:
            # stop here; next run retries from the same point
            log.error("Failed on post id=%s: %s -- will retry next run.",
                      max(x.id for x in grp), e)
            break

    save_cooldowns()   # remember which models are rate-limited for the next run
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
