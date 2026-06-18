"""
Watermarking: stamps your channel logo onto every photo and video before it is
re-posted to the Farsi channel.

How it works:
  - You put your logo file at  watermark/logo.png  (a transparent PNG is best).
  - Images are stamped with Pillow.
  - Videos are stamped with ffmpeg (re-encoded -- see note in README, it's slower).
  - The logo is placed in a corner, scaled relative to the media width.

Best-quality tip: design ONE logo PNG that already contains your text
("رادیو بولتن" + the logo) in a design tool, then save it as watermark/logo.png.
That way the Persian text is rendered perfectly and you avoid font/shaping issues.
"""

import os
import subprocess
import logging

log = logging.getLogger("radiobulletin.watermark")

ENABLED = os.environ.get("WATERMARK", "off").lower() in ("on", "1", "true", "yes")
LOGO_PATH = os.environ.get("LOGO_PATH", "watermark/logo.png")
POSITION = os.environ.get("WM_POSITION", "bottom-right").lower()  # 4 corners or center
LOGO_SCALE = float(os.environ.get("WM_SCALE", "0.16"))           # logo width as a fraction of media WIDTH
WM_MIN_W = int(os.environ.get("WM_MIN_W", "70"))                 # min logo width in px
WM_MAX_W = int(os.environ.get("WM_MAX_W", "700"))               # max logo width in px
LOGO_OPACITY = float(os.environ.get("WM_OPACITY", "0.85"))       # 0..1
MARGIN = int(os.environ.get("WM_MARGIN", "24"))                   # px from the edge


def _target_width(media_w: int, media_h: int) -> int:
    """Logo width = a fraction of the media WIDTH (consistent across resolutions),
    clamped so it's never absurdly small or large."""
    return int(round(max(WM_MIN_W, min(LOGO_SCALE * media_w, WM_MAX_W))))


def available() -> bool:
    """True only if watermarking is on AND the logo file actually exists."""
    if not ENABLED:
        return False
    if not os.path.exists(LOGO_PATH):
        log.warning("WATERMARK is on but logo not found at %s -- skipping.", LOGO_PATH)
        return False
    return True


def _xy(base_w, base_h, logo_w, logo_h):
    m = MARGIN
    if POSITION == "center":
        return (base_w - logo_w) // 2, (base_h - logo_h) // 2
    if POSITION == "top-left":
        return m, m
    if POSITION == "top-right":
        return base_w - logo_w - m, m
    if POSITION == "bottom-left":
        return m, base_h - logo_h - m
    return base_w - logo_w - m, base_h - logo_h - m  # bottom-right (default)


def _wm_image(in_path: str) -> str:
    from PIL import Image, ImageOps

    base = ImageOps.exif_transpose(Image.open(in_path)).convert("RGBA")  # honor phone rotation
    logo = Image.open(LOGO_PATH).convert("RGBA")

    target_w = _target_width(base.width, base.height)
    ratio = target_w / logo.width
    logo = logo.resize((target_w, max(1, round(logo.height * ratio))), Image.LANCZOS)

    if LOGO_OPACITY < 1.0:
        a = logo.split()[3].point(lambda p: int(p * LOGO_OPACITY))
        logo.putalpha(a)

    x, y = _xy(base.width, base.height, logo.width, logo.height)
    base.alpha_composite(logo, (x, y))

    out = in_path + ".wm.jpg"
    base.convert("RGB").save(out, "JPEG", quality=92)
    return out


def _wm_video(in_path: str) -> str:
    out = in_path + ".wm.mp4"
    overlay = {
        "center": "(W-w)/2:(H-h)/2",
        "top-left": f"{MARGIN}:{MARGIN}",
        "top-right": f"W-w-{MARGIN}:{MARGIN}",
        "bottom-left": f"{MARGIN}:H-h-{MARGIN}",
        "bottom-right": f"W-w-{MARGIN}:H-h-{MARGIN}",
    }.get(POSITION, f"W-w-{MARGIN}:H-h-{MARGIN}")

    # 1) Normalize the video to SQUARE pixels at its display size. This is the fix
    #    for the "stretched / thin / wrong-shape" logo: some videos have non-1:1
    #    pixels (SAR), which distorts anything overlaid on them. ih*dar = display width.
    # 2) Scale the logo to a fraction of that width (clamped), aspect preserved.
    # (commas inside expression functions are escaped with \\, for the filtergraph)
    w_expr = f"min(max(main_w*{LOGO_SCALE}\\,{WM_MIN_W})\\,{WM_MAX_W})"
    filt = (
        f"[0:v]scale=trunc(ih*dar/2)*2:ih:flags=lanczos,setsar=1[base];"
        f"[1:v]format=rgba,colorchannelmixer=aa={LOGO_OPACITY}[lg];"
        f"[lg][base]scale2ref=w={w_expr}:h=ow*ih/iw[wm][b2];"
        f"[b2][wm]overlay={overlay}"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", in_path, "-i", LOGO_PATH,
         "-filter_complex", filt, "-c:a", "copy",
         "-movflags", "+faststart", out],
        check=True,
        capture_output=True,
    )
    return out


def apply(in_path: str, kind: str) -> str:
    """Return path to a watermarked copy, or the original path on any problem
    (we never drop a post just because watermarking failed)."""
    if not available():
        return in_path
    try:
        if kind == "image":
            return _wm_image(in_path)
        if kind == "video":
            return _wm_video(in_path)
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg failed: %s", e.stderr.decode("utf-8", "ignore")[:500])
    except Exception as e:
        log.error("Watermarking failed (%s) -- using original.", e)
    return in_path
