"""
TrendDock Render — Layer 4b (public repo)
BuildWithSamad

Receives a candidate pool via repository_dispatch (client_payload =
decision.json contents), downloads + edits + uploads each video,
then calls back to the private repo to write results into posts_queue.

This repo is PUBLIC (unlimited GitHub Actions minutes) specifically to
absorb the heavy FFmpeg/Whisper compute cost. No Supabase credentials
live here — only Cloudinary (video hosting) and a scoped dispatch token
for calling back to the private repo.
"""

import json
import os
import re
import subprocess
import tempfile
import textwrap
import requests
from datetime import datetime
import cloudinary
import cloudinary.uploader
import cast_assets
import time
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
DECISION_FILE = "decision.json"
OUTPUT_DIR    = "processed"

PRIVATE_REPO   = "yekeensamad007-lang/trenddock"
CALLBACK_TOKEN = os.environ.get("CALLBACK_TOKEN")

# ── TikTok safe zones (1080 × 1920) ──────────────────────────────────────────
TT_W        = 1080
TT_H        = 1920
SAFE_TOP    = 140
SAFE_BOTTOM = 490
SAFE_RIGHT  = 160

# ── Processing config ─────────────────────────────────────────────────────────
PLAYBACK_SPEED   = 1.08
MAX_CLIP_SECONDS = 50
MIN_CLIP_SECONDS = 15

# ── Brand ─────────────────────────────────────────────────────────────────────
BRAND_ORANGE = "0xFF6B2B"

# ── TMDB visual treatment config ───────────────────────────────────────────────
CAPTION_CROP_RATIO  = 0.12   # fraction of frame height to crop off the bottom (trailer's own captions)
CARD_REVEAL_SECONDS = 0.6    # duration of the glassmorphic card's wipe-reveal animation
ACCOUNT_HANDLE      = os.environ.get("ACCOUNT_HANDLE", "@YourHandle")

TMDB_W = 1080
TMDB_H = 1350   # 4:5 — less extreme than 9:16, still vertical-friendly
CASTLINEUP_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "castlineup")
CASTLINEUP_PUBLIC = os.path.join(CASTLINEUP_DIR, "public")
CASTLINEUP_OUT   = os.path.join(CASTLINEUP_DIR, "out")
SLIDE_COUNT      = 5

# ── Font paths ────────────────────────────────────────────────────────────────
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/ubuntu/Ubuntu-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

# ── Cloudinary — pulled from environment, NEVER hardcoded in a public repo ────
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dnzxeped5"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
)

def _smart_truncate(text: str, max_len: int) -> str:
    """
    Truncates to at most max_len characters without cutting a word or
    sentence in half. Trims to the last full word boundary within the
    limit, then drops trailing dangling punctuation.
    """
    text = text.strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut
    
def paraphrase_description(overview: str, title: str) -> str:
    MAX_LEN = 180
    if not GEMINI_API_KEY:
        print("  GEMINI_API_KEY not set — using original overview (no Gemini attempt made)")
        return _smart_truncate(overview, MAX_LEN)
    if not overview:
        print("  No overview text provided — nothing to paraphrase")
        return overview

    prompt = (
        f"Rewrite this movie synopsis for '{title}' as a short, complete, "
        f"exciting summary suitable for a vertical social video caption. "
        f"It MUST be a full, coherent thought that ends with proper "
        f"punctuation — never cut off mid-sentence or mid-word. "
        f"Target length is 120-170 characters, but a complete sentence "
        f"matters more than hitting an exact count. "
        f"No spoilers beyond the premise. "
        f"Return ONLY the rewritten text, nothing else.\n\n"
        f"Original: {overview}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
    MAX_ATTEMPTS = 4
    backoff = 3  # seconds — doubles after each retry (3s, 6s, 12s)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=20
            )
            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not text:
                print("  Gemini returned an empty response — using original overview")
                return _smart_truncate(overview, MAX_LEN)
            print(f"  Gemini paraphrase SUCCEEDED (model: {GEMINI_MODEL}, attempt {attempt}/{MAX_ATTEMPTS})")
            return _smart_truncate(text, MAX_LEN + 40)

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text[:300] if e.response is not None else str(e)

            if status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                print(f"  Gemini paraphrase attempt {attempt}/{MAX_ATTEMPTS} FAILED — "
                      f"HTTP {status} (model: {GEMINI_MODEL}), retrying in {backoff}s: {body}")
                time.sleep(backoff)
                backoff *= 2
                continue

            print(f"  Gemini paraphrase FAILED — HTTP {status} (model: {GEMINI_MODEL}) "
                  f"after {attempt} attempt(s), giving up: {body}")
            return _smart_truncate(overview, MAX_LEN)

        except (requests.RequestException, KeyError, IndexError) as e:
            print(f"  Gemini paraphrase FAILED — {type(e).__name__}: {e} (model: {GEMINI_MODEL})")
            return _smart_truncate(overview, MAX_LEN)

    return _smart_truncate(overview, MAX_LEN)  # unreachable in practice, safety fallback


# ── Utilities ─────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s_\-]", "", text).strip()[:50]


def get_font() -> str:
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return ""


def load_decision():
    if not os.path.exists(DECISION_FILE):
        print("No decision.json found — nothing to render.")
        return None
    with open(DECISION_FILE, "r") as f:
        return json.load(f)


def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def run_cmd(cmd: list, label: str) -> bool:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAILED [{label}]:\n{result.stderr[-800:]}")
        return False
    print(f"OK [{label}]")
    return True


def has_audio_stream(path: str) -> bool:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return bool(result.stdout.strip())


def get_video_duration(path: str) -> float:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        try:
            return float(json.loads(result.stdout)["format"]["duration"])
        except (KeyError, ValueError, json.JSONDecodeError):
            pass
    return 30.0


# ── Bumper / logo-card removal ────────────────────────────────────────────────

def detect_bumper_segments(path: str) -> list:
    """
    Detects candidate non-content segments: genuine black frames AND
    static "frozen" cards (studio idents, rating cards, title cards)
    that hold with near-zero motion, regardless of color. Plain
    blackdetect misses colored logo cards (a blue Universal globe, a
    green MPA card) since they aren't black — freezedetect catches
    those by looking at motion instead of color.

    Does NOT catch cards with continuous animation (e.g. a rotating
    3D logo) — there's no cheap, reliable signal for that without
    actual scene/content classification. Accepted limitation.
    """
    segments = []

    black_cmd = [
        "ffmpeg", "-i", path,
        "-vf", "blackdetect=d=0.15:pic_th=0.90:pix_th=0.15",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(black_cmd, capture_output=True, text=True)
    for line in result.stderr.splitlines():
        if "black_start" in line:
            try:
                start = float(line.split("black_start:")[1].split()[0])
                end   = float(line.split("black_end:")[1].split()[0])
                segments.append((start, end))
            except (IndexError, ValueError):
                continue

    freeze_cmd = [
        "ffmpeg", "-i", path,
        "-vf", "freezedetect=n=-24dB:d=1.0",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(freeze_cmd, capture_output=True, text=True)
    freeze_start = None
    for line in result.stderr.splitlines():
        if "freeze_start:" in line:
            try:
                freeze_start = float(line.split("freeze_start:")[1].split()[0])
            except (IndexError, ValueError):
                freeze_start = None
        elif "freeze_end:" in line and freeze_start is not None:
            try:
                end = float(line.split("freeze_end:")[1].split()[0])
                segments.append((freeze_start, end))
            except (IndexError, ValueError):
                pass
            freeze_start = None

    return sorted(segments)


def trim_bumpers(input_path: str, video_id: str) -> str:
    """
    Strips leading and trailing runs of non-content (black frames,
    static logo/rating cards). Only trims a contiguous run starting
    at t=0 and a contiguous run ending at the final timestamp —
    deliberately ignores anything detected mid-video, since a real
    scene can legitimately cut to black for a beat, and removing that
    would risk cutting into actual content.

    Falls back to the original file untouched if nothing qualifies —
    a missed bumper is a smaller problem than an accidentally-cut scene.
    """
    total = get_video_duration(input_path)
    segments = detect_bumper_segments(input_path)

    if not segments:
        print("  No bumper/black segments detected — skipping trim")
        return input_path

    merged = []
    for seg in segments:
        if merged and seg[0] <= merged[-1][1] + 0.5:
            merged[-1] = (merged[-1][0], max(merged[-1][1], seg[1]))
        else:
            merged.append(seg)

    start_skip = 0.0
    for s, e in merged:
        if s <= start_skip + 0.5:
            start_skip = max(start_skip, e)
        else:
            break

    end_skip = total
    for s, e in reversed(merged):
        if e >= end_skip - 0.5:
            end_skip = min(end_skip, s)
        else:
            break

    if start_skip <= 0.0 and end_skip >= total:
        print("  No leading/trailing bumper run found — skipping trim")
        return input_path

    if end_skip - start_skip < MIN_CLIP_SECONDS:
        print("  Bumper trim would leave too little content — skipping trim")
        return input_path

    print(f"  Trimming bumpers: {start_skip:.1f}s from start, {total - end_skip:.1f}s from end")

    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_clean.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ss", f"{start_skip:.2f}", "-to", f"{end_skip:.2f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        output_path
    ]

    if run_cmd(cmd, "trim bumpers"):
        return output_path
    print("  Bumper trim failed — falling back to original clip")
    return input_path


# ── Smart, scene-aware trim ───────────────────────────────────────────────────

def detect_scene_cuts(path: str) -> list:
    """Returns timestamps (seconds) of hard scene changes."""
    cmd = [
        "ffmpeg", "-i", path,
        "-vf", "select='gt(scene,0.4)',showinfo",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    cuts = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
                cuts.append(t)
            except (IndexError, ValueError):
                continue
    return cuts


def get_smart_trim_point(path: str, target_max: float, speed: float = 1.0) -> float:
    """
    Finds a trim point in the ORIGINAL (pre-speed-up) footage such that
    AFTER the speed multiplier is applied, the final output lands at
    ~target_max seconds — not shorter. Without this compensation,
    trimming to target_max and then speeding up produces a shorter final
    video than intended (e.g. 50s trimmed + 1.08x speed = ~46s final).
    """
    adjusted_max = target_max * speed
    adjusted_min = MIN_CLIP_SECONDS * speed
    real = get_video_duration(path)

    if real <= adjusted_max:
        return max(real, adjusted_min)

    cuts = [c for c in detect_scene_cuts(path) if adjusted_min <= c <= adjusted_max]
    if cuts:
        return max(cuts)
    return adjusted_max


def _srt_timestamp(seconds: float) -> str:
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_captions(video_path: str):
    try:
        import whisper
        print("  Generating captions (Whisper tiny)...")
        model  = whisper.load_model("tiny")
        result = model.transcribe(video_path, fp16=False)
        fd, srt_path = tempfile.mkstemp(suffix=".srt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for i, seg in enumerate(result["segments"], 1):
                f.write(
                    f"{i}\n"
                    f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}\n"
                    f"{seg['text'].strip()}\n\n"
                )
        print("  Captions ready")
        return srt_path
    except ImportError:
        print("  Whisper not installed — skipping captions")
        return None
    except Exception as e:
        print(f"  Caption generation failed: {e}")
        return None
        
EMOJI_KEYWORDS = {
    "fire": "🔥", "crazy": "🤯", "love": "❤️", "money": "💰",
    "win": "🏆", "fight": "⚔️", "scary": "😱", "funny": "😂",
    "run": "🏃", "kill": "💀", "secret": "🤫", "shock": "😲",
    "power": "⚡", "war": "💥", "dream": "✨", "danger": "⚠️",
}


def _pick_emoji(word: str) -> str:
    clean = re.sub(r"[^a-z]", "", word.lower())
    return EMOJI_KEYWORDS.get(clean, "")


def generate_karaoke_captions(video_path: str):
    """
    Runs Whisper with word-level timestamps and builds an .ass subtitle
    file with karaoke-style progressive color-fill per word, positioned
    mid-frame, with occasional keyword-triggered emoji.

    Returns path to the .ass file, or None if Whisper isn't available
    or transcription fails — must never block rendering.
    """
    try:
        import whisper
        print("  Generating karaoke captions (Whisper tiny, word-level)...")
        model  = whisper.load_model("tiny")
        result = model.transcribe(video_path, fp16=False, word_timestamps=True)
    except ImportError:
        print("  Whisper not installed — skipping karaoke captions")
        return None
    except Exception as e:
        print(f"  Caption generation failed: {e}")
        return None

    fd, ass_path = tempfile.mkstemp(suffix=".ass")

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,DejaVu Sans Bold,58,&H00FFFFFF,&H0000A5FF,&H00000000,&H80000000,1,0,1,3,2,5,80,80,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def ts(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:d}:{m:02d}:{s:05.2f}"

    lines = []
    for seg in result.get("segments", []):
        words = seg.get("words", [])
        if not words:
            continue

        seg_start = words[0]["start"]
        seg_end   = words[-1]["end"]

        karaoke_text = ""
        for w in words:
            dur_cs = max(int((w["end"] - w["start"]) * 100), 1)  # centiseconds
            word_text = w["word"].strip()
            emoji = _pick_emoji(word_text)
            if emoji:
                word_text = f"{word_text} {emoji}"
            karaoke_text += f"{{\\k{dur_cs}}}{word_text} "

        lines.append(
            f"Dialogue: 0,{ts(seg_start)},{ts(seg_end)},Karaoke,,0,0,0,,{karaoke_text.strip()}"
        )

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))

    print(f"  Karaoke captions ready ({len(lines)} lines)")
    return ass_path

# ── Download ──────────────────────────────────────────────────────────────────

RAPIDAPI_YT_HOST = "youtube-video-fast-downloader-24-7.p.rapidapi.com"


def _extract_youtube_id(url: str) -> str:
    match = re.search(r"(?:v=|youtu\.be/)([a-zA-Z0-9_-]{11})", url)
    if not match:
        raise ValueError(f"Could not extract YouTube video ID from {url}")
    return match.group(1)


def _download_via_ytdlp(video_url: str, output_path: str) -> str | None:
    print(f"Downloading via yt-dlp: {output_path}")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo*+bestaudio*",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        video_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  yt-dlp download failed: {result.stderr[-500:]}")
        return None
    if not has_audio_stream(output_path):
        print(f"  yt-dlp result has no audio — abandoning")
        if os.path.exists(output_path):
            os.remove(output_path)
        return None
    print(f"Downloaded: {output_path}")
    return output_path


def _download_via_rapidapi(video_url: str, video_id: str, output_path: str) -> str | None:
    api_key = os.environ.get("RAPIDAPI_YT_KEY")
    if not api_key:
        print("RAPIDAPI_YT_KEY not set — cannot download")
        return None

    try:
        yt_id = _extract_youtube_id(video_url)
    except ValueError as e:
        print(f"  {e}")
        return None

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": RAPIDAPI_YT_HOST,
    }

    print(f"Requesting download link for {video_id}...")
    data = None
    for req_attempt in range(2):
        try:
            resp = requests.get(
                f"https://{RAPIDAPI_YT_HOST}/download_video/{yt_id}",
                headers=headers,
                params={"quality": "247"},
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            print(f"  RapidAPI request attempt {req_attempt + 1} failed: {e}")

    if data is None:
        print(f"  RapidAPI request failed after retry — giving up on {video_id}")
        return None

    file_url = data.get("file")
    backup_url = data.get("reserved_file")

    if not file_url:
        print(f"  No file URL in response: {data}")
        return None

    ready_url = None
    max_wait_seconds = 600
    poll_interval = 8
    elapsed = 0

    while elapsed < max_wait_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval
        try:
            head = requests.head(file_url, timeout=15)
            if head.status_code == 200:
                ready_url = file_url
                break
        except requests.RequestException:
            pass

        if backup_url:
            try:
                head = requests.head(backup_url, timeout=15)
                if head.status_code == 200:
                    ready_url = backup_url
                    break
            except requests.RequestException:
                pass

        print(f"  Still processing... ({elapsed}s elapsed)")

    if not ready_url:
        print(f"  File never became ready for {video_id} after {max_wait_seconds}s")
        return None

    file_url = ready_url

    print(f"Downloading {video_id}...")
    try:
        video_resp = requests.get(file_url, stream=True, timeout=60)
        video_resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in video_resp.iter_content(chunk_size=8192):
                f.write(chunk)
    except requests.RequestException as e:
        print(f"  Download failed: {e}")
        return None

    if not has_audio_stream(output_path):
        print(f"  {video_id}: downloaded file has no audio — abandoning")
        os.remove(output_path)
        return None

    print(f"Downloaded: {output_path}")
    return output_path


def download_video(video_url: str, video_id: str):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_raw.mp4")
    if os.path.exists(output_path):
        print(f"Already downloaded: {output_path}")
        if has_audio_stream(output_path):
            return output_path
        print(f"  Cached download has no audio — re-downloading {video_id}")
        os.remove(output_path)

    is_youtube = "youtube.com" in video_url or "youtu.be" in video_url
    if is_youtube:
        return _download_via_rapidapi(video_url, video_id, output_path)
    return _download_via_ytdlp(video_url, output_path)

def brand_main_video(
    input_path: str,
    video_id: str,
    author: str,
) -> str | None:
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_branded.mp4")
    if os.path.exists(output_path):
        print(f"Already branded: {output_path}")
        return output_path

    print("Branding main video...")
    font        = get_font()
    font_opt    = f"fontfile={font}:" if font else ""
    safe_author = clean_text(author)
    speed       = PLAYBACK_SPEED
    clip_dur    = get_smart_trim_point(input_path, MAX_CLIP_SECONDS, speed)

    print(f"  Clip: {clip_dur:.1f}s | Speed: {speed}x")

    filters = []
    filters.append(f"scale={TT_W}:{TT_H}:force_original_aspect_ratio=decrease")
    filters.append(f"pad={TT_W}:{TT_H}:(ow-iw)/2:(oh-ih)/2:color=black")
    filters.append(f"setpts=PTS/{speed}")
    filters.append("eq=contrast=1.22:saturation=1.55:brightness=0.03:gamma=1.08")
    filters.append(
        "curves="
        "r='0/0 0.25/0.22 0.50/0.52 0.75/0.80 1/1':"
        "g='0/0 0.25/0.21 0.50/0.51 0.75/0.79 1/1':"
        "b='0/0 0.25/0.19 0.50/0.49 0.75/0.77 1/1'"
    )
    filters.append("unsharp=5:5:0.7:5:5:0.0")
    filters.append("vignette=PI/5")

    credit_y = TT_H - SAFE_BOTTOM - 15
    filters.append(
        f"drawtext={font_opt}"
        f"text='Credit  @{safe_author}':"
        f"fontsize=30:fontcolor=white@0.80:"
        f"x=(w-text_w)/2:y={credit_y}:"
        f"borderw=2:bordercolor=black@0.65"
    )

    vf = ",".join(filters)

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(clip_dur),
        "-vf", vf,
        "-af", f"aresample=44100,atempo={speed}",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-ar", "44100",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    if not run_cmd(cmd, "brand main"):
        return None
    return output_path
    
# ── Brand main video ──────────────────────────────────────────────────────────

def build_tmdb_visual(input_path: str, video_id: str, title: str, overview: str) -> str | None:
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_tmdb_visual.mp4")
    if os.path.exists(output_path):
        return output_path

    font     = get_font()
    font_opt = f"fontfile={font}:" if font else ""

    speed    = PLAYBACK_SPEED
    clip_dur = get_smart_trim_point(input_path, MAX_CLIP_SECONDS, speed)
    print(f"  Clip: {clip_dur:.1f}s | Speed: {speed}x")

    safe_title    = clean_text(title)[:40]
    safe_overview = _smart_truncate(clean_text(overview), 160)

    desc_lines = textwrap.wrap(safe_overview, width=32) or [""]
    desc_lines = desc_lines[:4]
    desc_text  = "\n".join(desc_lines)

    fd, desc_file = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(desc_text)
    desc_file_escaped = desc_file.replace("\\", "/").replace(":", "\\:")

    title_fontsize = 54
    desc_fontsize  = 30
    desc_line_height = int(desc_fontsize * 1.3) + 8

    title_block_height = int(title_fontsize * 1.3)
    desc_block_height  = desc_line_height * len(desc_lines)
    pad_top, pad_gap, pad_bottom = 30, 20, 30

    card_top    = 160
    card_height = pad_top + title_block_height + pad_gap + desc_block_height + pad_bottom
    card_left   = 60
    card_width  = TMDB_W - 120

    title_y = card_top + pad_top
    desc_y  = title_y + title_block_height + pad_gap

    filter_complex = (
        f"[0:v]crop=iw:ih*{1 - CAPTION_CROP_RATIO}:0:0,hflip,split=2[src1][src2];"

        # Blurred, zoomed copy fills the full 9:16 canvas as background
        f"[src1]scale={TT_W}:{TT_H}:force_original_aspect_ratio=increase,"
        f"crop={TT_W}:{TT_H},boxblur=25:5,eq=brightness=-0.08,"
        f"setpts=PTS/{speed}[bg];"

        # Foreground keeps its native 4:5 shape, centered in the frame
        f"[src2]scale={TMDB_W}:{TMDB_H}:force_original_aspect_ratio=increase,"
        f"crop={TMDB_W}:{TMDB_H},"
        f"setpts=PTS/{speed}[fg];"

        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[merged];"

        f"[merged]drawbox="
        f"x={card_left}:y={card_top}:w={card_width}:h={card_height}:"
        f"color=black@0.35:thickness=fill[card_bg];"

        f"[card_bg]drawtext={font_opt}"
        f"text='{safe_title}':fontsize={title_fontsize}:fontcolor=white:"
        f"x={card_left + 30}:y={title_y}:"
        f"borderw=2:bordercolor=black@0.5[title_layer];"

        f"[title_layer]drawtext={font_opt}"
        f"textfile={desc_file_escaped}:fontsize={desc_fontsize}:fontcolor=white@0.90:"
        f"x={card_left + 30}:y={desc_y}:"
        f"line_spacing=8[desc_layer];"

        f"[desc_layer]drawbox="
        f"x='{card_left}+({card_width})*min(1,t/{CARD_REVEAL_SECONDS})':"
        f"y={card_top}:w={card_width}:h={card_height}:"
        f"color=black@1.0:thickness=fill[final]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-t", str(clip_dur),
        "-filter_complex", filter_complex,
        "-map", "[final]",
        "-map", "0:a:0?",
        "-af", f"aresample=44100,atempo={speed}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-ar", "44100",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    ok = run_cmd(cmd, "tmdb visual treatment")

    try:
        os.unlink(desc_file)
    except Exception:
        pass

    if not ok:
        return None
    return output_path


def apply_rotation(input_path: str, video_id: str, degrees: float = 2.5) -> str:
    """
    Applies a slight tilt to the whole frame, left or right, then crops
    back to the original canvas size to avoid black corner triangles
    the rotation would otherwise expose.
    """
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_rotated.mp4")
    angle_rad = degrees * 3.14159265 / 180

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", (
            f"rotate={angle_rad}:ow=rotw({angle_rad}):oh=roth({angle_rad}),"
            f"crop={TT_W}:{TT_H}:(iw-{TT_W})/2:(ih-{TT_H})/2"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "copy",
        output_path
    ]
    if run_cmd(cmd, "rotate"):
        return output_path
    return input_path

def burn_captions(input_path: str, video_id: str, caption_path: str) -> str:
    """
    Burns Whisper-generated captions into an already-sped, already-branded
    video. Must run LAST — after speed, color grading, and credit text —
    so caption timestamps (generated by Whisper on this exact sped file)
    match the final timeline exactly. No scaling/adjustment needed, since
    Whisper transcribed the file at its final playback speed.
    """
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_captioned.mp4")
    if os.path.exists(output_path):
        print(f"Already captioned: {output_path}")
        return output_path

    caption_escaped = caption_path.replace("\\", "/").replace(":", "\\:")
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", f"subtitles={caption_escaped}",
        "-c:a", "copy",
        output_path
    ]
    if run_cmd(cmd, "burn captions"):
        return output_path
    print("  Caption burn failed — using uncaptioned branded video")
    return input_path

# ── Outro card ────────────────────────────────────────────────────────────────

def generate_outro(video_id: str, author: str, title: str = "", overview: str = ""):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_outro.mp4")
    if os.path.exists(output_path):
        print(f"Already have outro: {output_path}")
        return output_path

    font        = get_font()
    font_opt    = f"fontfile={font}:" if font else ""
    safe_author = clean_text(author)

    print("Generating designed outro...")

    bg = (
        f"[0:v]geq="
        f"r='40+20*sin(2*PI*Y/H)':"
        f"g='20+10*sin(2*PI*Y/H)':"
        f"b='60+25*sin(2*PI*Y/H)'"
    )

    sweep1 = (
        f"drawbox=x=0:y='(ih/2)-60':w='iw*min(1,t/0.35)':h=6:"
        f"color={BRAND_ORANGE}@0.90:thickness=fill"
    )
    sweep2 = (
        f"drawbox=x='iw*max(0,1-min(1,(t-0.10)/0.35))':y='(ih/2)+60':w='iw':h=6:"
        f"color={BRAND_ORANGE}@0.55:thickness=fill"
    )

    has_title_block = bool(title.strip())
    desc_files = []  # track temp files for cleanup

    if has_title_block:
        safe_title = clean_text(title)[:40]

        title_text = (
            f"drawtext={font_opt}text='{safe_title}':fontsize=52:fontcolor=white:"
            f"x=(w-text_w)/2:y='h/2-360':"
            f"alpha='min(1,max(0,(t-0.15)/0.35))':"
            f"borderw=2:bordercolor=black@0.5"
        )

        safe_overview = clean_text(overview)[:160]
        desc_lines = textwrap.wrap(safe_overview, width=32)[:3] or [""]
        desc_text  = "\n".join(desc_lines)

        fd, desc_file = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(desc_text)
        desc_files.append(desc_file)
        desc_file_escaped = desc_file.replace("\\", "/").replace(":", "\\:")

        desc_text_filter = (
            f"drawtext={font_opt}textfile={desc_file_escaped}:fontsize=28:"
            f"fontcolor=white@0.85:x=(w-text_w)/2:y='h/2-280':"
            f"line_spacing=6:alpha='min(1,max(0,(t-0.30)/0.35))'"
        )
    else:
        title_text = None
        desc_text_filter = None

    # Vertically centered between sweep1/sweep2 using ffmpeg's own
    # text_h, so it sits inside the two lines regardless of fontsize —
    # no more manual pixel-offset guessing. Handle and credit removed:
    # TMDB doesn't own the movie, so no credit line makes sense here,
    # and the handle placeholder was never meant to ship as-is.
    follow = (
        f"drawtext={font_opt}text='FOLLOW FOR MORE':fontsize=48:fontcolor=white:"
        f"x=(w-text_w)/2:y='h/2-text_h/2':"
        f"alpha='min(1,max(0,(t-0.20)/0.40))'"
    )

    layers = [bg, sweep1, sweep2]
    if title_text:
        layers.append(title_text)
    if desc_text_filter:
        layers.append(desc_text_filter)
    layers.append(follow)

    vf = ",".join(layers)

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x0D0D0D:size={TT_W}x{TT_H}:rate=30",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "3.5",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path
    ]

    ok = run_cmd(cmd, "outro")

    for f in desc_files:
        try:
            os.unlink(f)
        except Exception:
            pass

    if not ok:
        return None
    return output_path


# ── Concatenate ───────────────────────────────────────────────────────────────

def concatenate(video_id: str, main_path: str, outro_path: str):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_final.mp4")
    if os.path.exists(output_path):
        print(f"Already concatenated: {output_path}")
        return output_path

    concat_file = os.path.join(OUTPUT_DIR, f"{video_id}_concat.txt")
    print("Concatenating main + outro...")
    with open(concat_file, "w") as f:
        f.write(f"file '{os.path.abspath(main_path)}'\n")
        f.write(f"file '{os.path.abspath(outro_path)}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output_path
    ]
    if not run_cmd(cmd, "concatenate"):
        return None
    return output_path


# ── Cloudinary upload ─────────────────────────────────────────────────────────

def upload_to_cloudinary(final_path: str, video_id: str):
    print(f"Uploading to Cloudinary: {final_path}")
    if not os.path.exists(final_path):
        print(f"File not found: {final_path}")
        return None, None

    result = cloudinary.uploader.upload(
        final_path,
        resource_type="video",
        folder="trenddock",
        public_id=f"trenddock_{video_id}_{int(datetime.now().timestamp())}",
        use_filename=False,
        unique_filename=False,
        eager=[{"start_offset": "1.0", "format": "jpg"}],
        eager_async=False
    )

    url = result.get("secure_url")
    print(f"Cloudinary URL: {url}")

    eager_result = result.get("eager", [])
    if eager_result:
        thumbnail_url = eager_result[0].get("secure_url")
        print(f"Thumbnail pre-generated: {thumbnail_url}")
    else:
        print("Eager thumbnail generation returned no result — falling back to lazy URL")
        thumbnail_url = url.replace("/upload/", "/upload/so_1.0/", 1)
        if thumbnail_url.endswith(".mp4"):
            thumbnail_url = thumbnail_url[:-4] + ".jpg"

    return url, thumbnail_url


# ── Callback to private repo ──────────────────────────────────────────────────

def send_result_to_private_repo(post_number, video_id, author, niche, caption, cloudinary_url, thumbnail_url):
    if not CALLBACK_TOKEN:
        print("CALLBACK_TOKEN not set — cannot report result back to private repo!")
        return False

    payload = {
        "event_type": "render_complete",
        "client_payload": {
            "rank":          post_number,
            "video_id":      video_id,
            "author":        author,
            "niche":         niche,
            "caption":       caption,
            "cloudinary_url": cloudinary_url,
            "thumbnail_url": thumbnail_url
        }
    }
    resp = requests.post(
        f"https://api.github.com/repos/{PRIVATE_REPO}/dispatches",
        headers={
            "Authorization": f"Bearer {CALLBACK_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json=payload,
        timeout=15
    )
    if resp.status_code == 204:
        print(f"Reported rank #{post_number} back to private repo")
        return True
    print(f"Callback failed ({resp.status_code}): {resp.text[:200]}")
    return False


# ── Per-video pipeline ────────────────────────────────────────────────────────

def is_tmdb_source(video: dict) -> bool:
    """TMDB trailer candidates use video_id format 'tmdb_{movie_id}' — see tmdb_trailer.py."""
    return str(video.get("video_id", "")).startswith("tmdb_")


def process_video(video: dict, post_number: int) -> bool:
    """Router — dispatches to the cast-lineup, TMDB, or TikTok processing path."""
    if is_castlineup_source(video):
        return process_castlineup_video(video, post_number)
    if is_tmdb_source(video):
        return process_tmdb_video(video, post_number)
    return process_tiktok_video(video, post_number)
    
def has_burned_in_captions(video_path: str, samples: int = 5) -> bool:
    """
    Samples several frames across the video and OCRs the lower-middle
    band where captions conventionally sit. If text is reliably detected
    across a majority of sampled frames, treats the clip as already
    captioned — used to skip redundant Whisper captioning on TikTok
    clips that already have burned-in text.

    Heuristic, not perfect — occasional false positives/negatives are
    expected. Errs toward returning False (run Whisper anyway) if
    frame sampling itself fails, since a missing caption is a smaller
    problem than a wrongly-skipped one.
    """
    import cv2
    import pytesseract

    duration = get_video_duration(video_path)
    if duration <= 0:
        return False

    hits, checked = 0, 0

    for i in range(samples):
        t = duration * (i + 1) / (samples + 1)
        frame_path = os.path.join(OUTPUT_DIR, f"_ocr_sample_{i}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", str(t), "-i", video_path,
            "-frames:v", "1", "-q:v", "2", frame_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 or not os.path.exists(frame_path):
            continue

        checked += 1
        img = cv2.imread(frame_path)
        if img is not None:
            h, w = img.shape[:2]
            # lower-middle band — roughly bottom third, center 80% of width
            band = img[int(h * 0.65):int(h * 0.95), int(w * 0.10):int(w * 0.90)]
            text = pytesseract.image_to_string(band).strip()
            if len(text) >= 3:
                hits += 1

        try:
            os.unlink(frame_path)
        except Exception:
            pass

    if checked == 0:
        print("  OCR caption check: could not sample frames — assuming no captions")
        return False

    ratio = hits / checked
    detected = ratio >= 0.5
    print(f"  OCR caption check: {hits}/{checked} frames show text ({ratio:.0%}) — burned-in captions: {detected}")
    return detected

def process_tiktok_video(video: dict, post_number: int) -> bool:
    video_id    = video["video_id"]
    video_url   = video["video_url"]
    description = video.get("description", "Trending Now")
    author      = video.get("author", "unknown")
    niche       = video.get("niche", "")
    cand_rank   = video.get("rank", "?")

    print(f"\n{'='*50}")
    print(f"TrendDock Render (TikTok) — Candidate #{cand_rank} → Post #{post_number}")
    print(f"Author : @{author} | Niche: {niche}")
    print(f"{'='*50}\n")

    raw_path = download_video(video_url, video_id)
    if not raw_path:
        return False

    print(f"  Raw download has audio: {has_audio_stream(raw_path)}")

    cleaned_path = trim_bumpers(raw_path, video_id)

    branded_path = brand_main_video(cleaned_path, video_id, author)
    if not branded_path:
        return False

    # Detection runs on cleaned_path (pre-brand) — we're checking whether
    # the ORIGINAL source clip already has burned-in captions, which has
    # nothing to do with our own credit-text overlay. Running this check
    # on branded_path instead would pick up the "Credit @author" text
    # (it sits inside the same OCR sample band) and falsely report every
    # video as already-captioned, silently disabling Whisper for everything.
    #
    # Whisper transcription still runs on branded_path — that part is
    # correct and unchanged, since it needs final-timeline timestamps.
    if has_burned_in_captions(cleaned_path):
        print("  Clip already has burned-in captions — skipping Whisper")
        srt_path = None
    else:
        srt_path = generate_karaoke_captions(branded_path)

    final_path = burn_captions(branded_path, video_id, srt_path) if srt_path else branded_path

    print(f"  Final output has audio: {has_audio_stream(final_path)}")

    if srt_path and os.path.exists(srt_path):
        try:
            os.unlink(srt_path)
        except Exception:
            pass

    cloudinary_url, thumbnail_url = upload_to_cloudinary(final_path, video_id)
    if not cloudinary_url:
        print(f"Cloudinary upload failed for {video_id}")
        return False

    caption = f"{description[:100]} | via @TrendDock"

    ok = send_result_to_private_repo(
        post_number, video_id, author, niche, caption, cloudinary_url, thumbnail_url
    )
    if not ok:
        print(f"WARNING: video rendered but callback failed for {video_id} — posts_queue will NOT have this entry")

    print(f"\nCandidate #{cand_rank} → Post #{post_number} done")
    return ok


def process_tmdb_video(video: dict, post_number: int) -> bool:
    """
    Stage 3 stub — full visual treatment built out in Stage 5.
    Deliberately does NOT call generate_captions() at all: official
    trailers already have burned-in captions we plan to remove and
    replace with our own designed caption system later — running
    Whisper here now would be wasted work on captions we're not using yet.
    """
    video_id    = video["video_id"]
    video_url   = video["video_url"]
    title       = video.get("description", "Trending Now")
    niche       = video.get("niche", "movietrailer")
    cand_rank   = video.get("rank", "?")

    print(f"\n{'='*50}")
    print(f"TrendDock Render (TMDB) — Candidate #{cand_rank} → Post #{post_number}")
    print(f"Title : {title}")
    print(f"{'='*50}\n")

    raw_path = download_video(video_url, video_id)
    if not raw_path:
        return False

    cleaned_path = trim_bumpers(raw_path, video_id)

    raw_overview = video.get("overview", "")
    overview     = paraphrase_description(raw_overview, title)
    branded_path = build_tmdb_visual(cleaned_path, video_id, title, overview)
    if not branded_path:
        return False

    ass_path = generate_karaoke_captions(branded_path)

    if ass_path:
        captioned_path = os.path.join(OUTPUT_DIR, f"{video_id}_captioned.mp4")
        ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        cmd = [
            "ffmpeg", "-y", "-i", branded_path,
            "-vf", f"subtitles={ass_escaped}",
            "-c:a", "copy",
            captioned_path
        ]
        branded_path = captioned_path if run_cmd(cmd, "burn tmdb captions") else branded_path

    outro_path = generate_outro(video_id, "TMDB", title=title, overview=overview)
    if not outro_path:
        return False

    final_path = concatenate(video_id, branded_path, outro_path)
    if not final_path:
        return False

    cloudinary_url, thumbnail_url = upload_to_cloudinary(final_path, video_id)
    if not cloudinary_url:
        print(f"Cloudinary upload failed for {video_id}")
        return False

    caption = f"{overview} | via @TrendDock"

    ok = send_result_to_private_repo(
        post_number, video_id, "TMDB", niche, caption, cloudinary_url, thumbnail_url
    )
    if not ok:
        print(f"WARNING: video rendered but callback failed for {video_id}")

    print(f"\nCandidate #{cand_rank} → Post #{post_number} done")
    return ok
    def is_castlineup_source(video: dict) -> bool:
    """Cast-lineup candidates use video_id format 'castlineup_{movie_id}' — see decision.py."""
    return str(video.get("video_id", "")).startswith("castlineup_")
 
 
def _stage_image_for_remotion(local_path: str, dest_filename: str) -> str | None:
    """
    Copies a locally-downloaded/processed image into castlineup/public/
    so Remotion's staticFile() can serve it during render. Returns the
    filename to pass as the prop value (staticFile() takes a filename
    relative to public/, not a full path).
    """
    if not local_path or not os.path.exists(local_path):
        return None
    os.makedirs(CASTLINEUP_PUBLIC, exist_ok=True)
    dest_path = os.path.join(CASTLINEUP_PUBLIC, dest_filename)
    with open(local_path, "rb") as src, open(dest_path, "wb") as dst:
        dst.write(src.read())
    return dest_filename
 
 
def prepare_castlineup_props(assets: dict, movie_id: int) -> dict:
    """
    Takes cast_assets.build_cast_lineup_assets()'s output (local
    filesystem paths) and stages everything into castlineup/public/,
    returning a props dict with FILENAMES (not paths) — the shape
    CastLineup.tsx expects, since it resolves them via staticFile().
    """
    staged_members = []
    for i, member in enumerate(assets["members"]):
        filename = None
        if member.get("imageUrl"):
            ext = os.path.splitext(member["imageUrl"])[1] or ".jpg"
            dest_name = f"{movie_id}_cast{i}{ext}"
            filename = _stage_image_for_remotion(member["imageUrl"], dest_name)
 
        staged_members.append({
            "name":      member["name"],
            "character": member["character"],
            "tagline":   member.get("tagline", ""),
            "imageUrl":  filename,
        })
 
    logo_filename = None
    if assets.get("movieLogoUrl"):
        logo_filename = _stage_image_for_remotion(
            assets["movieLogoUrl"], f"{movie_id}_logo.png"
        )
 
    return {
        "members":      staged_members,
        "movieTitle":   assets["movieTitle"],
        "movieLogoUrl": logo_filename,
    }
 
 
def render_castlineup_slide(props: dict, featured_index: int, video_id: str, slide_num: int) -> str | None:
    """
    Invokes Remotion's CLI to render ONE still (one featured member)
    via subprocess. Requires Node.js + npm dependencies already
    installed in castlineup/ — see render.yml (still pending as of
    this writing; will fail in CI until that's added).
    """
    ensure_output_dir()
    os.makedirs(CASTLINEUP_OUT, exist_ok=True)
    output_path = os.path.join(CASTLINEUP_OUT, f"{video_id}_slide{slide_num}.png")
 
    slide_props = dict(props)
    slide_props["featuredIndex"] = featured_index
 
    cmd = [
        "npx", "remotion", "still",
        "src/index.ts", "CastLineup",
        output_path,
        "--props", json.dumps(slide_props),
    ]
 
    result = subprocess.run(cmd, cwd=CASTLINEUP_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"FAILED [render slide {slide_num}]:\n{result.stderr[-800:]}")
        return None
    print(f"OK [render slide {slide_num}] -> {output_path}")
    return output_path
 
 
def upload_image_to_cloudinary(image_path: str, video_id: str, slide_num: int) -> str | None:
    """Carousel slides upload as resource_type='image', not 'video' — a genuinely different Cloudinary call than upload_to_cloudinary()."""
    if not os.path.exists(image_path):
        print(f"Slide image not found: {image_path}")
        return None
    result = cloudinary.uploader.upload(
        image_path,
        resource_type="image",
        folder="trenddock/castlineup",
        public_id=f"castlineup_{video_id}_{slide_num}_{int(datetime.now().timestamp())}",
        use_filename=False,
        unique_filename=False,
    )
    return result.get("secure_url")
 
 
def send_carousel_result_to_private_repo(post_number, video_id, movie_title, caption, media_urls: list) -> bool:
    """
    Carousel equivalent of send_result_to_private_repo() — sends a
    media_urls LIST instead of one cloudinary_url + thumbnail_url.
 
    NOT YET FUNCTIONAL end-to-end: posts_queue's schema and poster.py's
    Make.com payload still only handle a single video_url per row (per
    earlier conversation — this was flagged as a dependency, not yet
    built). This function will successfully dispatch the event, but
    the private repo's receiving workflow needs a matching update
    before a carousel actually reaches posts_queue correctly. Said
    plainly so this isn't mistaken for a finished pipeline.
    """
    if not CALLBACK_TOKEN:
        print("CALLBACK_TOKEN not set — cannot report carousel result back to private repo!")
        return False
 
    payload = {
        "event_type": "render_complete_carousel",
        "client_payload": {
            "rank":       post_number,
            "video_id":   video_id,
            "author":     movie_title,
            "niche":      "movietrailer",
            "caption":    caption,
            "media_urls": media_urls,
        }
    }
    resp = requests.post(
        f"https://api.github.com/repos/{PRIVATE_REPO}/dispatches",
        headers={
            "Authorization": f"Bearer {CALLBACK_TOKEN}",
            "Accept": "application/vnd.github+json"
        },
        json=payload,
        timeout=15
    )
    if resp.status_code == 204:
        print(f"Reported carousel rank #{post_number} back to private repo")
        return True
    print(f"Carousel callback failed ({resp.status_code}): {resp.text[:200]}")
    return False
 
 
def process_castlineup_video(video: dict, post_number: int) -> bool:
    video_id  = video["video_id"]
    movie_id  = video.get("_tmdb_movie_id")
    cast      = video.get("cast", [])
    cand_rank = video.get("rank", "?")
 
    print(f"\n{'='*50}")
    print(f"TrendDock Render (Cast Lineup) — Candidate #{cand_rank} -> Post #{post_number}")
    print(f"{'='*50}\n")
 
    if not movie_id or not cast:
        print(f"  Missing movie_id or cast list for {video_id} — cannot build carousel")
        return False
 
    candidate_for_assets = {
        "_tmdb_movie_id": movie_id,
        "_tmdb_title":    video.get("_tmdb_title", ""),
        "cast":           cast,
    }
    assets = cast_assets.build_cast_lineup_assets(candidate_for_assets)
    props  = prepare_castlineup_props(assets, movie_id)
 
    media_urls = []
    for i in range(min(SLIDE_COUNT, len(props["members"]))):
        slide_path = render_castlineup_slide(props, i, video_id, i)
        if not slide_path:
            print(f"  Slide {i} failed to render — aborting carousel (partial carousels aren't posted)")
            return False
        slide_url = upload_image_to_cloudinary(slide_path, video_id, i)
        if not slide_url:
            print(f"  Slide {i} failed to upload — aborting carousel")
            return False
        media_urls.append(slide_url)
 
    caption = f"Meet the cast of {assets['movieTitle']} | via @TrendDock"
 
    ok = send_carousel_result_to_private_repo(
        post_number, video_id, assets["movieTitle"], caption, media_urls
    )
    if not ok:
        print(f"WARNING: carousel rendered but callback failed for {video_id}")
 
    print(f"\nCandidate #{cand_rank} -> Post #{post_number} done (carousel, {len(media_urls)} slides)")
    return ok

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("TrendDock Render (public repo) — Layer 4b")
    print("=" * 50)

    decision = load_decision()
    if not decision:
        return

    ensure_output_dir()

    pool   = decision.get("candidates", [])
    target = decision.get("target_posts", 4)

    if not pool:
        print("No candidates found in decision.json")
        return

    print(f"\nTarget  : {target} posts")
    print(f"Pool    : {len(pool)} candidates\n")

    success   = 0
    attempted = 0

    for video in pool:
        if success >= target:
            break

        attempted += 1
        print(f"\n── Candidate #{video.get('rank', attempted)} | Post slot {success + 1}/{target} ──")

        ok = process_video(video, success + 1)

        if ok:
            success += 1
            print(f"{success}/{target} posts complete")
        else:
            remaining = len(pool) - attempted
            print(f"Skipped — {success}/{target} done | {remaining} candidates left")

    print(f"\n{'='*50}")
    if success >= target:
        print(f"RENDER DONE — {success}/{target} posts sent to private repo")
    else:
        print(f"PARTIAL — {success}/{target} posts completed")
        if attempted >= len(pool):
            print(f"Pool exhausted after {len(pool)} candidates.")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
