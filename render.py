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
import requests
from datetime import datetime
import cloudinary
import cloudinary.uploader

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


# ── Black-frame / title-card removal ─────────────────────────────────────────

def detect_black_segments(path: str) -> list:
    """
    Finds black-frame ranges (studio idents, title cards) so we can cut
    them out before branding — teasers commonly pad these in.
    Returns list of (start, end) tuples in seconds.
    """
    cmd = [
        "ffmpeg", "-i", path,
        "-vf", "blackdetect=d=0.3:pic_th=0.98",
        "-an", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    segments = []
    for line in result.stderr.splitlines():
        if "black_start" in line:
            try:
                start = float(line.split("black_start:")[1].split()[0])
                end   = float(line.split("black_end:")[1].split()[0])
                segments.append((start, end))
            except (IndexError, ValueError):
                continue
    return segments


def remove_black_segments(input_path: str, video_id: str) -> str:
    """
    Cuts out detected black-frame ranges (title cards, studio idents)
    and returns a path to the cleaned video. If no black segments are
    found, or the cut fails for any reason, returns the ORIGINAL path
    unchanged — this step must never block the rest of the pipeline.
    """
    total = get_video_duration(input_path)
    black = detect_black_segments(input_path)

    if not black:
        print("  No black-frame segments found — skipping cleanup")
        return input_path

    keep = []
    cursor = 0.0
    for start, end in sorted(black):
        if start > cursor:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < total:
        keep.append((cursor, total))

    if not keep:
        print("  Black-segment removal would leave nothing — skipping cleanup")
        return input_path

    print(f"  Removing {len(black)} black-frame segment(s), keeping {len(keep)} clean range(s)")

    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_clean.mp4")
    select_expr = "+".join(f"between(t,{s:.2f},{e:.2f})" for s, e in keep)

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"select='{select_expr}',setpts=N/FRAME_RATE/TB",
        "-af", f"aselect='{select_expr}',asetpts=N/SR/TB",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        output_path
    ]

    if run_cmd(cmd, "remove black segments"):
        return output_path

    print("  Black-frame removal failed — falling back to original clip")
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


# ── Download ──────────────────────────────────────────────────────────────────

def download_video(video_url: str, video_id: str):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_raw.mp4")
    if os.path.exists(output_path):
        print(f"Already downloaded: {output_path}")
        return output_path
    print(f"Downloading {video_id}...")
    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", output_path,
        "--no-playlist",
        video_url
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Download failed: {result.stderr[-500:]}")
        return None
    print(f"Downloaded: {output_path}")
    return output_path


# ── Brand main video ──────────────────────────────────────────────────────────

def brand_main_video(input_path: str, video_id: str, author: str, srt_path=None):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_branded.mp4")
    if os.path.exists(output_path):
        print(f"Already branded: {output_path}")
        return output_path

    print("Branding main video...")
    font        = get_font()
    font_opt    = f"fontfile={font}:" if font else ""
    safe_author = clean_text(author)
    clip_dur    = get_smart_trim_point(input_path, MAX_CLIP_SECONDS, speed)
    speed       = PLAYBACK_SPEED

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

    if srt_path and os.path.exists(srt_path):
        srt_escaped    = srt_path.replace("\\", "/").replace(":", "\\:")
        caption_margin = SAFE_BOTTOM + 70
        filters.append(
            f"subtitles={srt_escaped}:"
            f"force_style='"
            f"FontName=DejaVu Sans Bold,"
            f"FontSize=48,"
            f"PrimaryColour=&HFFFFFF,"
            f"OutlineColour=&H000000,"
            f"BackColour=&H70000000,"
            f"Bold=1,Outline=3,Shadow=2,"
            f"Alignment=2,MarginV={caption_margin}'"
        )

    credit_y = TT_H - SAFE_BOTTOM - 15
    filters.append(
        f"drawtext={font_opt}"
        f"text='Credit  @{safe_author}':fontsize=30:fontcolor=white@0.80:"
        f"x=(w-text_w)/2:y={credit_y}:borderw=2:bordercolor=black@0.65"
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


# ── Outro card ────────────────────────────────────────────────────────────────

def generate_outro(video_id: str, author: str):
    output_path = os.path.join(OUTPUT_DIR, f"{video_id}_outro.mp4")
    if os.path.exists(output_path):
        print(f"Already have outro: {output_path}")
        return output_path

    font        = get_font()
    font_opt    = f"fontfile={font}:" if font else ""
    safe_author = clean_text(author)

    print("Generating animated outro...")

    sweep = (
        f"drawbox=x=0:y='(ih-10)/2':w='iw*min(1,t/0.40)':h=10:"
        f"color={BRAND_ORANGE}@0.95:thickness=fill"
    )
    follow_y = "h/2-text_h/2-190-65*max(0,1-max(0,(t-0.20)/0.50))"
    follow = (
        f"drawtext={font_opt}text='Follow':fontsize=76:fontcolor=white:"
        f"x=(w-text_w)/2:y='{follow_y}':alpha='min(1,max(0,(t-0.20)/0.40))'"
    )
    handle_x = "(w-text_w)/2+(w+text_w)*max(0,1-max(0,(t-0.50)/0.50))"
    handle = (
        f"drawtext={font_opt}text='@TrendDock':fontsize=86:fontcolor={BRAND_ORANGE}:"
        f"x='{handle_x}':y='h/2-text_h/2-45':"
        f"alpha='min(1,max(0,(t-0.50)/0.45))':borderw=3:bordercolor=white@0.25"
    )
    tagline = (
        f"drawtext={font_opt}text='For daily fire content':fontsize=38:"
        f"fontcolor={BRAND_ORANGE}@0.85:x=(w-text_w)/2:y='h/2+92':"
        f"alpha='min(1,max(0,(t-0.90)/0.40))'"
    )
    credit = (
        f"drawtext={font_opt}text='Credit  @{safe_author}':fontsize=28:"
        f"fontcolor=white@0.45:x=(w-text_w)/2:y='h/2+178':"
        f"alpha='min(1,max(0,(t-1.30)/0.40))'"
    )

    vf = ",".join([sweep, follow, handle, tagline, credit])

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

    if not run_cmd(cmd, "outro"):
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
    """Router — dispatches to the TikTok or TMDB processing path."""
    if is_tmdb_source(video):
        return process_tmdb_video(video, post_number)
    return process_tiktok_video(video, post_number)


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

    cleaned_path = remove_black_segments(raw_path, video_id)

    # Stage 4: skip Whisper if this clip already has burned-in captions
    if has_burned_in_captions(cleaned_path):
        print("  Clip already has burned-in captions — skipping Whisper")
        srt_path = None
    else:
        srt_path = generate_captions(cleaned_path)

    branded_path = brand_main_video(cleaned_path, video_id, author, srt_path)
    if not branded_path:
        return False

    outro_path = generate_outro(video_id, author)
    if not outro_path:
        return False

    final_path = concatenate(video_id, branded_path, outro_path)
    if not final_path:
        return False

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

    cleaned_path = remove_black_segments(raw_path, video_id)

    # Stage 5 replaces this with the stretch/blur/glassmorphic treatment
    branded_path = brand_main_video(cleaned_path, video_id, "TMDB", None)
    if not branded_path:
        return False

    outro_path = generate_outro(video_id, "TMDB")
    if not outro_path:
        return False

    final_path = concatenate(video_id, branded_path, outro_path)
    if not final_path:
        return False

    cloudinary_url, thumbnail_url = upload_to_cloudinary(final_path, video_id)
    if not cloudinary_url:
        print(f"Cloudinary upload failed for {video_id}")
        return False

    caption = f"{title[:100]} | via @TrendDock"

    ok = send_result_to_private_repo(
        post_number, video_id, "TMDB", niche, caption, cloudinary_url, thumbnail_url
    )
    if not ok:
        print(f"WARNING: video rendered but callback failed for {video_id}")

    print(f"\nCandidate #{cand_rank} → Post #{post_number} done")
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
