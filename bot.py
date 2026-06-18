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
from datetime import datetime

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
# Prepended to a post that doesn't already start with an emoji.
LEAD_EMOJI = os.environ.get("LEAD_EMOJI", "🔹")
# The English channel's @username (without @). Used to scrub it from the text so
# the English id never leaks into the Farsi channel. Optional but recommended.
SOURCE_USERNAME = os.environ.get("SOURCE_USERNAME", "").lstrip("@").lower()

# Per-post analytics report channel (set to "" to disable). The bot account must
# be able to post there.
LOG_CHANNEL = os.environ.get("LOG_CHANNEL", "@AnalyzeAistrb")
REPORT_TZ = os.environ.get("REPORT_TZ", "Asia/Tehran")

TRANSLATOR = os.environ.get("TRANSLATOR", "github").lower()   # github | google
GH_TOKEN = os.environ.get("GH_MODELS_TOKEN", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

# Translation fallback chain, BEST Persian first. Each entry is "backend:model".
# Backends:
#   github : GitHub Models (needs GH_MODELS_TOKEN)   -> keeps HTML formatting
#   gemini : Google Gemini (needs GEMINI_API_KEY)    -> keeps HTML formatting, big daily quota
#   google : free Google Translate (no key)          -> plain text only, final safety net
# When a model hits its limit the bot moves to the next one. Edit freely; if a
# model id is wrong/retired the bot just skips it. Non-OpenAI ids can be copied
# from https://github.com/marketplace/models
_DEFAULT_CHAIN = (
    "github:openai/gpt-4.1,"
    "github:openai/gpt-4o,"
    "gemini:gemini-2.5-flash,"
    "github:openai/gpt-4.1-mini,"
    "github:openai/gpt-4o-mini,"
    "github:deepseek/DeepSeek-V3-0324,"
    "github:meta/Llama-3.3-70B-Instruct,"
    "github:mistral-ai/Mistral-Large-2411,"
    "google:translate"
)


def _parse_chain(s: str):
    chain = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        backend, _, model = part.partition(":")
        chain.append({"name": part, "backend": backend.strip().lower(), "model": model.strip()})
    return chain


CHAIN = _parse_chain(os.environ.get("CHAIN", _DEFAULT_CHAIN))

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

# Telegram limits (UTF-16 code units): media caption 1024, message text 4096.
CAPTION_LIMIT = int(os.environ.get("CAPTION_LIMIT", "1024"))
TEXT_LIMIT = int(os.environ.get("TEXT_LIMIT", "4096"))
# A post that keeps failing is skipped after this many attempts, so one bad post
# can never permanently block the channel.
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
FAILS_FILE = pathlib.Path(os.environ.get("FAILS_FILE", "state/failures.json"))

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


def _looks_persian(text: str) -> bool:
    """True if the (tag-stripped) text is meaningfully Persian. Used to reject a
    model that ignored the instruction and echoed the English text back."""
    plain = re.sub(r"<[^>]+>", "", text or "")
    letters = [c for c in plain if c.isalpha()]
    if not letters:
        return True  # only emoji/numbers/punctuation -> nothing to translate
    persian = sum(
        1 for c in letters
        if "\u0600" <= c <= "\u06FF" or "\u0750" <= c <= "\u077F"
        or "\uFB50" <= c <= "\uFDFF" or "\uFE70" <= c <= "\uFEFF"
    )
    return persian / len(letters) >= 0.3


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


def _load_fails() -> dict:
    try:
        return json.loads(FAILS_FILE.read_text())
    except Exception:
        return {}


def save_fails():
    try:
        FAILS_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAILS_FILE.write_text(json.dumps(_FAILS, sort_keys=True))
    except Exception as e:
        log.warning("Could not save failure counts: %s", e)


_FAILS = _load_fails()


def _retry_after(e) -> int:
    try:
        return int(e.headers.get("retry-after", "0") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _chat_completion(url: str, headers: dict, model: str, text: str) -> str:
    """One OpenAI-compatible chat call (works for both GitHub Models and Gemini)."""
    body = json.dumps({
        "model": model,
        "temperature": 0.4,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**headers, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return _strip_fences(data["choices"][0]["message"]["content"])


def _call_backend(entry: dict, html_text: str, plain_text: str):
    """Dispatch to the right backend. Returns (text, is_html)."""
    backend, model = entry["backend"], entry["model"]
    if backend == "github":
        return _chat_completion(
            "https://models.github.ai/inference/chat/completions",
            {"Authorization": f"Bearer {GH_TOKEN}",
             "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2026-03-10"},
            model, html_text), True
    if backend == "gemini":
        return _chat_completion(
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            {"Authorization": f"Bearer {GEMINI_KEY}"},
            model, html_text), True
    if backend == "google":
        return _translate_google(plain_text), False
    raise ValueError(f"unknown backend: {backend}")


def _available(entry: dict) -> bool:
    if entry["backend"] == "github":
        return bool(GH_TOKEN)
    if entry["backend"] == "gemini":
        return bool(GEMINI_KEY)
    return entry["backend"] == "google"   # always available (no key)


def translate_chain(html_text: str, plain_text: str):
    """Walk the chain best-first, skipping anything on cooldown or non-Persian.
    Returns (translated_text, is_html, model_name). Raises if nothing worked."""
    now = time.time()
    last_err = None
    for entry in CHAIN:
        name = entry["name"]
        if not _available(entry) or _COOLDOWNS.get(name, 0) > now:
            continue
        try:
            text, is_html = _call_backend(entry, html_text, plain_text)
            if not _looks_persian(text):
                log.warning("%s returned non-Persian output; trying next.", name)
                continue   # quality miss (not a rate limit) -> just try next, no cooldown
            return text, is_html, name
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                wait = _retry_after(e)
                if 0 < wait <= 20:   # brief throttle: wait once and retry same backend
                    time.sleep(wait + 1)
                    try:
                        text, is_html = _call_backend(entry, html_text, plain_text)
                        if _looks_persian(text):
                            return text, is_html, name
                    except Exception as e2:
                        last_err = e2
                cd = min(wait or COOLDOWN_DEFAULT, COOLDOWN_MAX)
                _COOLDOWNS[name] = now + cd
                log.warning("%s rate-limited; cooling %ss, switching to next.", name, cd)
            elif 400 <= e.code < 500:
                _COOLDOWNS[name] = now + COOLDOWN_MAX   # bad/retired model id
                log.warning("%s returned HTTP %s; disabling temporarily.", name, e.code)
            else:
                _COOLDOWNS[name] = now + 60             # 5xx transient
                log.warning("%s returned HTTP %s; brief cooldown.", name, e.code)
        except Exception as e:
            last_err = e
            _COOLDOWNS[name] = now + 60                 # network / timeout
            log.warning("%s call failed (%s); brief cooldown.", name, e)
    raise RuntimeError(f"all translators unavailable ({last_err})")


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


# Covers 🔹 (U+1F537) and the common emoji/pictograph/symbol/flag blocks.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # pictographs, emoticons, transport, extended-A
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows (★ ⬛ …)
    "\U00002190-\U000021FF"   # arrows
    "]"
)


def _starts_with_emoji(text: str) -> bool:
    plain = re.sub(r"<[^>]+>", "", text or "")                 # ignore leading HTML tags
    plain = plain.lstrip(" \t\r\n\u200c\u200e\u200f\ufeff")    # and whitespace/zero-width
    return bool(plain) and bool(_EMOJI_RE.match(plain))


def _ensure_lead_emoji(body: str) -> str:
    """Prepend the default emoji if the post doesn't already start with one."""
    if body and not _starts_with_emoji(body):
        return f"{LEAD_EMOJI} {body}"
    return body


def build_caption(html_text: str, plain_text: str):
    """Return (caption, model_used). Caption is HTML (parse_mode=html): translated
    body with source formatting, a leading emoji, and a BOLD footer."""
    bold_footer = f"<b>{html_lib.escape(FOOTER)}</b>"

    if not html_text.strip() and not (plain_text or "").strip():
        return bold_footer, None          # media-only post, no text

    try:
        fa, is_html, model_used = translate_chain(html_text, plain_text)
        if not is_html:                    # google plain text -> make it valid HTML
            fa = html_lib.escape(fa)
    except Exception as e:
        log.error("All translators failed (%s). Posting original text.", e)
        fa = html_lib.escape(plain_text or "")
        model_used = "original (untranslated)"

    fa = _ensure_lead_emoji(_scrub_source(fa).strip())
    caption = f"{fa}\n\n{bold_footer}" if fa else bold_footer
    return caption, model_used


def _now_str() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(REPORT_TZ)).strftime("%Y-%m-%d %H:%M ") + REPORT_TZ
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


async def send_report(client, model_used, src_id, sent):
    """Best-effort analytics report to LOG_CHANNEL. Never raises into the caller."""
    if not LOG_CHANNEL:
        return
    msg = sent[0] if isinstance(sent, (list, tuple)) and sent else sent
    handle = TARGET_HANDLE.lstrip("@")
    link = ""
    if msg is not None and handle and not handle.lstrip("-").isdigit():
        link = f"https://t.me/{handle}/{getattr(msg, 'id', '')}"
    lines = [
        "📊 <b>گزارش انتشار</b>",
        f"منبع: {html_lib.escape(str(SOURCE_CHANNEL))}",
        f"مدل زبانی: <code>{html_lib.escape(model_used or '— (بدون ترجمه)')}</code>",
        f"تاریخ و ساعت: {html_lib.escape(_now_str())}",
        (f'<a href="{link}">مشاهده‌ی پست</a>' if link else f"شناسه‌ی پست مبدأ: {src_id}"),
    ]
    await client.send_message(LOG_CHANNEL, "\n".join(lines), link_preview=False)


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


async def _send_text(client, target, text, reply_to=None):
    while True:
        try:
            return await client.send_message(target, text, link_preview=False, reply_to=reply_to)
        except FloodWaitError as e:
            if e.seconds > MAX_FLOOD:
                log.warning("FloodWait %ss exceeds cap; stopping run, will resume next trigger.", e.seconds)
                raise
            log.warning("FloodWait: sleeping %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)


def _tg_len(s: str) -> int:
    """Telegram counts message/caption length in UTF-16 code units, ignoring the
    HTML tags (they become entities). This measures the visible length the same way."""
    plain = re.sub(r"<[^>]+>", "", s or "")
    return len(plain.encode("utf-16-le")) // 2


def _split_plain(text: str, limit: int):
    """Split plain text into <=limit (UTF-16) chunks at line/space boundaries."""
    chunks, buf = [], ""
    for line in (text or "").split("\n"):
        piece = (buf + "\n" + line) if buf else line
        if len(piece.encode("utf-16-le")) // 2 <= limit:
            buf = piece
        else:
            if buf:
                chunks.append(buf)
            # a single very long line: hard-cut by characters
            while len(line.encode("utf-16-le")) // 2 > limit:
                cut = line[:limit]
                chunks.append(cut)
                line = line[limit:]
            buf = line
    if buf:
        chunks.append(buf)
    return chunks or [""]


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


async def _send_long_text(client, target, html_text, reply_to=None):
    """Send text that may exceed the 4096 limit. Keeps HTML formatting when it fits
    in one message; for longer text falls back to plain split (formatting lost only
    in that rare case)."""
    if _tg_len(html_text) <= TEXT_LIMIT:
        return await _send_text(client, target, html_text, reply_to=reply_to)
    plain = re.sub(r"<[^>]+>", "", html_text)
    first = None
    for chunk in _split_plain(plain, TEXT_LIMIT):
        m = await _send_text(client, target, html_lib.escape(chunk), reply_to=reply_to)
        first = first or m
    return first


async def post_group(client, target, group, caption):
    """Post one post (single message or album), keeping ALL media. If watermarking
    is on, every photo/video is stamped first. If the caption is longer than the
    media caption limit, the media goes out first and the full text follows as a
    reply (so nothing is lost). Returns the primary sent message."""
    media_msgs = [m for m in group if m.media]

    # Text-only post
    if not media_msgs:
        return await _send_long_text(client, target, caption)

    # Resolve the media to send (re-use, or download + watermark)
    files = None
    tmpdir = None
    if not watermark.available():
        try:
            raw = [m.media for m in media_msgs]
            files = raw if len(raw) > 1 else raw[0]
        except Exception as e:
            log.warning("Direct media re-send prep failed (%s); will download.", e)
    if files is None:
        tmpdir = tempfile.TemporaryDirectory()
        paths = []
        for m in media_msgs:
            p = await m.download_media(file=tmpdir.name + "/")
            if not p:
                continue
            if watermark.available():
                p = watermark.apply(p, media_kind(m))
            paths.append(p)
        if not paths:
            tmpdir.cleanup()
            return await _send_long_text(client, target, caption)
        files = paths if len(paths) > 1 else paths[0]

    try:
        if _tg_len(caption) <= CAPTION_LIMIT:
            sent = await _send_file(client, target, files, caption)
        else:
            # caption too long for a media caption: send media first, then the
            # full text as a reply so nothing is lost.
            sent = await _send_file(client, target, files, None)
            primary = sent[0] if isinstance(sent, (list, tuple)) and sent else sent
            await _send_long_text(client, target, caption, reply_to=getattr(primary, "id", None))
    finally:
        if tmpdir:
            tmpdir.cleanup()
    return sent


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
            caption, model_used = build_caption(html_text, plain_text)
            sent = await post_group(client, tgt, grp, caption)
            # checkpoint AFTER success -> nothing lost, nothing duplicated
            post_id = max(x.id for x in grp)
            write_state(post_id)
            _FAILS.pop(str(post_id), None)   # clear any prior failure count
            log.info("Mirrored post id=%s (%d media) via %s.",
                     post_id, sum(1 for x in grp if x.media), model_used or "—")
            try:
                await send_report(client, model_used, post_id, sent)
            except Exception as e:
                log.warning("Report to %s failed: %s", LOG_CHANNEL, e)
            await asyncio.sleep(POST_DELAY)
        except FloodWaitError as e:
            # Telegram is throttling the account. This is TEMPORARY, so we must not
            # treat it as a bad post: stop the run and resume next time. Nothing lost.
            log.warning("FloodWait reached; stopping run, will resume next trigger (%s).", e)
            break
        except Exception as e:
            post_id = max(x.id for x in grp)
            n = _FAILS.get(str(post_id), 0) + 1
            _FAILS[str(post_id)] = n
            if n >= MAX_ATTEMPTS:
                # one genuinely bad post must never block the channel forever -> skip it
                log.error("Skipping post id=%s after %d failed attempts: %s", post_id, n, e)
                write_state(post_id)
                _FAILS.pop(str(post_id), None)
                continue
            log.error("Failed on post id=%s (attempt %d/%d): %s -- will retry next run.",
                      post_id, n, MAX_ATTEMPTS, e)
            break

    save_cooldowns()   # remember which models are rate-limited for the next run
    save_fails()       # remember failing posts (for the skip-after-N safeguard)
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run())
