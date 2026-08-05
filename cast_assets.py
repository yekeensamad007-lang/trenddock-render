"""
Cast Lineup — Asset Preparation
TrendDock / BuildWithSamad — Layer 4b (public repo, alongside render.py)

Takes the `cast` list embedded in a decision.json candidate with
video_id "castlineup_{movie_id}" (built by decision.py's
build_cast_lineup_candidate(), sourced from cast_fetcher.fetch_top_cast())
and turns it into everything CastLineup.tsx needs:

  1. Downloads each cast member's TMDB profile photo (resolves the
     relative profile_path against TMDB's image CDN — cast_fetcher.py
     deliberately left this step for here, see its own docstring).
  2. Runs rembg (u2net_human_seg) on each photo to produce a
     transparent-background cutout PNG.
  3. Fetches a transparent movie logo via TMDB's /movie/{id}/images,
     preferring English PNG logos, highest-voted first.
  4. Generates a one-line Gemini character tagline per cast member,
     using the SAME retry/backoff/fallback pattern as
     paraphrase_description() in render.py, so failure behavior is
     consistent across the pipeline.

NOTE — what this module does NOT do yet: get these file paths into
Remotion's renderer. Remotion (headless Chromium) can't load arbitrary
local filesystem paths the way a Python script can — images need to be
served (e.g. copied into the Remotion project's public/ folder and
referenced via staticFile(), or served over a local HTTP server during
render). That wiring is part of the render.py `castlineup_` branch,
built next — not solved here. This module's contract ends at "here are
local file paths on disk," which is genuinely useful and testable on
its own, but is not the finish line.
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_BASE    = "https://api.themoviedb.org/3"
TMDB_IMG_CDN = "https://image.tmdb.org/t/p"

CAST_PHOTO_SIZE = "w500"
LOGO_SIZE       = "w500"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

OUTPUT_DIR = "processed/castlineup"

# rembg model session — loaded once and reused across all 5 cast members
# per movie. Loading the model fresh per-call would be needlessly slow;
# this matches the "load once, reuse" pattern any ML model session needs.
_REMBG_SESSION = None


def _ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


# ── Cast photo download ───────────────────────────────────────────────────────

def download_cast_photo(profile_path: str, movie_id: int, member_index: int) -> str | None:
    """
    Resolves a TMDB-relative profile_path (e.g. "/abc123.jpg", as
    returned by cast_fetcher.fetch_top_cast) against TMDB's image CDN
    and downloads it. Cached — re-running the pipeline on the same
    movie/member won't re-download.
    """
    _ensure_output_dir()
    output_path = os.path.join(OUTPUT_DIR, f"{movie_id}_cast{member_index}_raw.jpg")
    if os.path.exists(output_path):
        print(f"  Already downloaded: {output_path}")
        return output_path

    url = f"{TMDB_IMG_CDN}/{CAST_PHOTO_SIZE}{profile_path}"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Cast photo download failed ({profile_path}): {e}")
        return None

    with open(output_path, "wb") as f:
        f.write(resp.content)
    print(f"  Downloaded cast photo: {output_path}")
    return output_path


# ── Background removal (rembg) ────────────────────────────────────────────────

def _get_rembg_session():
    global _REMBG_SESSION
    if _REMBG_SESSION is None:
        from rembg import new_session
        print("  Loading rembg model (u2net_human_seg)...")
        _REMBG_SESSION = new_session("u2net_human_seg")
    return _REMBG_SESSION


def remove_background(input_path: str, movie_id: int, member_index: int) -> str | None:
    """
    Runs rembg on a downloaded cast photo, producing a transparent PNG
    cutout. Returns None on failure (missing rembg install, or a
    genuine processing error) rather than raising — callers must have a
    fallback (using the original, un-cutout photo) since this runs
    unattended with no human review before posting.
    """
    _ensure_output_dir()
    output_path = os.path.join(OUTPUT_DIR, f"{movie_id}_cast{member_index}_cutout.png")
    if os.path.exists(output_path):
        print(f"  Already have cutout: {output_path}")
        return output_path

    try:
        from rembg import remove
    except ImportError:
        print("  rembg not installed — cannot produce cutout. "
              "Add 'rembg' and 'onnxruntime' to requirements.txt.")
        return None

    try:
        session = _get_rembg_session()
        with open(input_path, "rb") as f:
            input_bytes = f.read()
        output_bytes = remove(input_bytes, session=session)
    except Exception as e:
        print(f"  rembg background removal failed for {input_path}: {e}")
        return None

    with open(output_path, "wb") as f:
        f.write(output_bytes)
    print(f"  Cutout ready: {output_path}")
    return output_path


# ── Movie logo ─────────────────────────────────────────────────────────────────

def fetch_movie_logo(movie_id: int) -> str | None:
    """
    Fetches a transparent PNG movie logo via TMDB /movie/{id}/images.
    Prefers English-language logos, highest vote_average first, PNG
    only (need real transparency for compositing). Returns None if no
    suitable logo exists — CastLineup.tsx falls back to styled text in
    that case, per the confirmed spec.
    """
    if not TMDB_API_KEY:
        print("  TMDB_API_KEY not set — skipping logo fetch")
        return None

    _ensure_output_dir()
    output_path = os.path.join(OUTPUT_DIR, f"{movie_id}_logo.png")
    if os.path.exists(output_path):
        print(f"  Already have logo: {output_path}")
        return output_path

    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{movie_id}/images",
            params={"api_key": TMDB_API_KEY},
            timeout=15
        )
        resp.raise_for_status()
        logos = resp.json().get("logos", [])
    except requests.RequestException as e:
        print(f"  TMDB logo fetch failed: {e}")
        return None

    png_logos = [l for l in logos if l.get("file_path", "").lower().endswith(".png")]
    if not png_logos:
        print("  No PNG logos available for this movie — will use text-title fallback")
        return None

    english = [l for l in png_logos if l.get("iso_639_1") == "en"]
    candidates = english if english else png_logos
    candidates.sort(key=lambda l: l.get("vote_average", 0), reverse=True)
    chosen = candidates[0]

    url = f"{TMDB_IMG_CDN}/{LOGO_SIZE}{chosen['file_path']}"
    try:
        img_resp = requests.get(url, timeout=20)
        img_resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  Logo image download failed: {e}")
        return None

    with open(output_path, "wb") as f:
        f.write(img_resp.content)
    print(f"  Logo downloaded: {output_path}")
    return output_path


# ── Character tagline (Gemini) ──────────────────────────────────────────────────

def generate_character_tagline(actor_name: str, character_name: str, movie_title: str) -> str:
    """
    One-line Gemini-generated character description, same retry/backoff
    pattern as render.py's paraphrase_description(). Returns "" on
    failure — CastLineup.tsx must render cleanly with just name +
    character when this is empty (drop the tagline line silently),
    matching the agreed graceful-degradation behavior rather than
    blocking a slide over one bad API response.
    """
    if not GEMINI_API_KEY:
        print("  GEMINI_API_KEY not set — skipping tagline generation")
        return ""

    prompt = (
        f"Write a punchy, one-sentence description (under 12 words) of the "
        f"character '{character_name}' played by {actor_name} in the movie "
        f"'{movie_title}', suitable for a social media cast-reveal card. "
        f"No spoilers beyond their role/archetype. "
        f"Return ONLY the sentence, nothing else."
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
    MAX_ATTEMPTS = 4
    backoff = 3

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
                print(f"  Gemini tagline empty for {character_name} — dropping tagline")
                return ""
            print(f"  Tagline generated for {character_name} (attempt {attempt}/{MAX_ATTEMPTS})")
            return text[:120]  # hard safety cap, well above the ~70-char target

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                print(f"  Tagline attempt {attempt}/{MAX_ATTEMPTS} failed — HTTP {status}, retrying in {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            print(f"  Tagline generation FAILED for {character_name} — HTTP {status}, giving up")
            return ""

        except (requests.RequestException, KeyError, IndexError) as e:
            print(f"  Tagline generation FAILED for {character_name} — {type(e).__name__}: {e}")
            return ""

    return ""


# ── Orchestrator ─────────────────────────────────────────────────────────────

def build_cast_lineup_assets(cast_lineup_candidate: dict) -> dict:
    """
    Takes the castlineup_{movie_id} candidate dict as written into
    decision.json (must have "cast", "_tmdb_movie_id", "_tmdb_title").
    Returns a dict shaped to match CastLineup.tsx's props directly:

        {
            "members": [
                {"name", "character", "tagline", "imageUrl"}, ... x5
            ],
            "movieTitle": str,
            "movieLogoUrl": str | None,
        }

    "imageUrl" here is a LOCAL FILESYSTEM PATH (cutout if rembg
    succeeded, otherwise the raw downloaded photo as a fallback) — not
    yet a URL Remotion can load. See module docstring for what's still
    unresolved before this reaches the renderer.
    """
    movie_id = cast_lineup_candidate["_tmdb_movie_id"]
    title    = cast_lineup_candidate["_tmdb_title"]
    cast     = cast_lineup_candidate.get("cast", [])

    print(f"\nBuilding cast lineup assets for '{title}' (movie_id={movie_id})")

    members = []
    for i, member in enumerate(cast):
        name      = member.get("name", "")
        character = member.get("character", "")
        profile   = member.get("profile_path")

        print(f"\n[{i+1}/{len(cast)}] {name} as {character}")

        raw_path = download_cast_photo(profile, movie_id, i) if profile else None
        image_path = None
        if raw_path:
            image_path = remove_background(raw_path, movie_id, i)
            if not image_path:
                print(f"  Falling back to raw (non-cutout) photo for {name}")
                image_path = raw_path

        tagline = generate_character_tagline(name, character, title)

        members.append({
            "name":      name,
            "character": character,
            "tagline":   tagline,
            "imageUrl":  image_path,
        })

    logo_path = fetch_movie_logo(movie_id)

    return {
        "members":      members,
        "movieTitle":   title,
        "movieLogoUrl": logo_path,
    }
