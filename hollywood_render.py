"""Heavy analysis and rendering stages for Hollywood explainers.

The private repository sends only Cloudinary URLs and job metadata. This worker
keeps video bytes, Whisper work, voice generation, FFmpeg, and finished assets
on the public GitHub Actions runner.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cloudinary
import cloudinary.uploader
import requests

PRIVATE_REPO = "yekeensamad007-lang/trenddock"
OUTPUT_DIR = Path("processed/hollywood")
VOICE = os.environ.get("PIPER_VOICE", "en_US-lessac-medium")
VOICE_DIR = Path(os.environ.get("PIPER_DATA_DIR", "piper_voices"))


def required(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required Actions secret/environment variable: {name}")
    return value


def configure_cloudinary():
    cloudinary.config(
        cloud_name=required("CLOUDINARY_CLOUD_NAME"),
        api_key=required("CLOUDINARY_API_KEY"),
        api_secret=required("CLOUDINARY_API_SECRET"),
        secure=True,
    )


def event_payload():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as handle:
        return json.load(handle).get("client_payload") or {}


def download(url, path):
    with requests.get(url, stream=True, timeout=(30, 180)) as response:
        response.raise_for_status()
        with open(path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
    if not os.path.getsize(path):
        raise RuntimeError(f"Downloaded empty asset from {url}")


def parse_vtt(text):
    cues = []
    current = None
    time_re = re.compile(
        r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+"
        r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
    )

    def seconds(groups, offset):
        hours = int(groups[offset] or 0)
        minutes = int(groups[offset + 1])
        secs = int(groups[offset + 2])
        millis = int(groups[offset + 3])
        return hours * 3600 + minutes * 60 + secs + millis / 1000

    for line in text.splitlines():
        match = time_re.search(line)
        if match:
            groups = match.groups()
            current = {"start": seconds(groups, 0), "end": seconds(groups, 4), "text": ""}
            cues.append(current)
            continue
        line = re.sub(r"<[^>]+>", "", line).strip()
        if current and line and not line.startswith(("WEBVTT", "NOTE", "STYLE")):
            current["text"] = (current["text"] + " " + line).strip()
    return [cue for cue in cues if cue["text"] and cue["end"] > cue["start"]]


def scene_cuts(video_path):
    command = [
        "ffmpeg", "-i", str(video_path), "-vf", "select='gt(scene,0.32)',showinfo",
        "-an", "-f", "null", "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    cuts = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            try:
                cuts.append(float(line.split("pts_time:", 1)[1].split()[0]))
            except (IndexError, ValueError):
                pass
    return sorted(set(cuts))


def duration(video_path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True, timeout=60,
    )
    result.check_returncode()
    return float(result.stdout.strip())


def transcript_text_to_cues(text, total_seconds):
    """Use Agent Reach transcript text without running Whisper when timed VTT is unavailable."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    if not sentences and text.strip():
        sentences = [text.strip()]
    if not sentences:
        return []
    weights = [max(1, len(re.findall(r"\w+", sentence))) for sentence in sentences]
    total_weight = sum(weights)
    elapsed = 0.0
    cues = []
    for sentence, weight in zip(sentences, weights):
        start = elapsed
        elapsed += total_seconds * weight / total_weight
        cues.append({"start": start, "end": elapsed, "text": sentence})
    return cues


def transcribe(video_path):
    import whisper

    model = whisper.load_model(os.environ.get("WHISPER_MODEL", "base"))
    result = model.transcribe(str(video_path), fp16=False, verbose=False)
    return [
        {"start": float(item["start"]), "end": float(item["end"]), "text": item["text"].strip()}
        for item in result.get("segments", [])
        if item.get("text", "").strip()
    ]


def upload_json(payload, public_id):
    configure_cloudinary()
    manifest = OUTPUT_DIR / f"{public_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = cloudinary.uploader.upload(
        str(manifest), resource_type="raw",
        folder="trenddock/hollywood/manifests", public_id=public_id, overwrite=True,
    )
    return result.get("secure_url")


def callback(event_type, payload):
    response = requests.post(
        f"https://api.github.com/repos/{PRIVATE_REPO}/dispatches",
        headers={
            "Authorization": f"Bearer {required('CALLBACK_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"event_type": event_type, "client_payload": payload},
        timeout=30,
    )
    if response.status_code != 204:
        raise RuntimeError(f"Private callback failed: {response.status_code} {response.text[:400]}")


def analyze():
    payload = event_payload()
    job_id = payload["job_id"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="td-hollywood-analysis-") as temp:
        video = Path(temp) / "source.mp4"
        download(payload["source_url"], video)
        video_duration = duration(video)
        cues = []
        subtitle_source = None
        if payload.get("subtitles_url"):
            subtitle_path = Path(temp) / "source.vtt"
            try:
                download(payload["subtitles_url"], subtitle_path)
                cues = parse_vtt(subtitle_path.read_text(encoding="utf-8", errors="replace"))
                if cues:
                    subtitle_source = "youtube"
            except Exception as exc:
                print(f"Timestamped YouTube subtitles could not be read: {exc}")
        if not cues and payload.get("transcript_url"):
            transcript_path = Path(temp) / "agent_reach_transcript.txt"
            try:
                download(payload["transcript_url"], transcript_path)
                transcript_text = transcript_path.read_text(encoding="utf-8", errors="replace")
                cues = transcript_text_to_cues(transcript_text, video_duration)
                if cues:
                    subtitle_source = "agent_reach"
            except Exception as exc:
                print(f"Agent Reach transcript could not be read: {exc}")
        if not cues:
            print("No usable YouTube subtitles or Agent Reach transcript; running local Whisper.")
            cues = transcribe(video)
            subtitle_source = "whisper"
        if not cues:
            raise RuntimeError("Neither YouTube subtitles nor Whisper produced transcript cues.")

        analysis = {
            "job_id": job_id,
            "duration_seconds": video_duration,
            "subtitle_source": subtitle_source,
            "transcript": cues,
            "scene_cuts": scene_cuts(video),
        }
        analysis_url = upload_json(analysis, f"analysis_{job_id}")
    callback("hollywood_analysis_complete", {"job_id": job_id, "analysis_url": analysis_url})
    print(f"Analysis ready for {job_id}: {len(cues)} cues, source={subtitle_source}.")


def run_ffmpeg(command, label):
    result = subprocess.run(command, capture_output=True, text=True, timeout=7200)
    if result.returncode:
        raise RuntimeError(f"{label} failed: {result.stderr[-2000:]}")


def make_voiceover(text, wav_path):
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if not (VOICE_DIR / f"{VOICE}.onnx").exists():
        subprocess.run(
            [sys.executable, "-m", "piper.download_voices", VOICE, "--data-dir", str(VOICE_DIR)],
            check=True, timeout=900,
        )
    subprocess.run(
        [
            sys.executable, "-m", "piper", "-m", VOICE,
            "--data-dir", str(VOICE_DIR), "-f", str(wav_path), "--", text,
        ],
        check=True, timeout=1800,
    )


def split_sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]


def write_srt(script, total_seconds, path):
    sentences = split_sentences(script)
    if not sentences:
        raise RuntimeError("The judge returned no narration for an episode.")
    weights = [max(1, len(re.findall(r"\w+", sentence))) for sentence in sentences]
    total_weight = sum(weights)
    elapsed = 0.0
    rows = []
    for index, (sentence, weight) in enumerate(zip(sentences, weights), start=1):
        start = elapsed
        elapsed += total_seconds * weight / total_weight
        end = min(total_seconds, elapsed)
        def stamp(value):
            ms = int(round(value * 1000))
            h, rem = divmod(ms, 3_600_000)
            m, rem = divmod(rem, 60_000)
            s, ms = divmod(rem, 1000)
            return f"{h:02}:{m:02}:{s:02},{ms:03}"
        rows.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n{sentence}\n")
    Path(path).write_text("\n".join(rows), encoding="utf-8")


def render_episode(source, episode, episode_index, job_id, title, music_path, output_dir):
    segments = episode.get("segments") or []
    narration = " ".join((segment.get("narration") or "").strip() for segment in segments).strip()
    words = len(re.findall(r"\w+", narration))
    if not 850 <= words <= 1500:
        raise RuntimeError(f"Episode {episode_index} narration is {words} words; judge must return 850–1500.")
    video_parts = []
    for i, segment in enumerate(segments):
        start, end = float(segment["start"]), float(segment["end"])
        part = output_dir / f"ep{episode_index}_part{i}.mp4"
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        run_ffmpeg([
            "ffmpeg", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(source),
            "-vf", vf, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", "30", str(part),
        ], f"scene segment {i}")
        video_parts.append(part)

    concat_file = output_dir / f"ep{episode_index}_concat.txt"
    concat_file.write_text(
        "".join(f"file '{str(part.resolve()).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for part in video_parts),
        encoding="utf-8",
    )
    video_only = output_dir / f"ep{episode_index}_video.mp4"
    run_ffmpeg([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-c", "copy", str(video_only),
    ], "join selected scenes")

    voice = output_dir / f"ep{episode_index}_voice.wav"
    make_voiceover(narration, voice)
    voice_seconds = duration(voice)
    video_seconds = duration(video_only)
    if not 360 <= video_seconds <= 540:
        raise RuntimeError(
            f"Episode {episode_index} rendered duration is {video_seconds:.0f}s; expected 360–540s."
        )
    ratio = voice_seconds / video_seconds
    if ratio < 0.8 or ratio > 1.25:
        raise RuntimeError(
            f"Episode {episode_index} narration duration ({voice_seconds:.0f}s) does not fit "
            f"selected footage ({video_seconds:.0f}s). Judge must rebalance script and scenes."
        )

    captions = output_dir / f"ep{episode_index}.srt"
    write_srt(narration, video_seconds, captions)
    final_path = output_dir / f"ep{episode_index}_final.mp4"
    escaped_caption_path = str(captions.resolve()).replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles={escaped_caption_path}"
    music_gain = float(os.environ.get("HOLLYWOOD_MUSIC_GAIN", "0.10"))
    run_ffmpeg([
        "ffmpeg", "-y", "-i", str(video_only), "-i", str(voice), "-stream_loop", "-1",
        "-i", str(music_path), "-filter_complex",
        f"[1:a]atempo={ratio:.5f}[voice];[2:a]volume={music_gain}[music];"
        "[voice][music]amix=inputs=2:duration=first:dropout_transition=2[a]",
        "-vf", vf, "-map", "0:v:0", "-map", "[a]", "-t", f"{video_seconds:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", str(final_path),
    ], "narration, music, and caption mix")
    return final_path, video_seconds


def render():
    payload = event_payload()
    job_id = payload["job_id"]
    plan = payload["edit_plan"]
    title = payload.get("title") or "Movie Explainer"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    configure_cloudinary()
    music_url = os.environ.get("HOLLYWOOD_MUSIC_URL", "").strip()
    if not music_url:
        raise RuntimeError("Set the HOLLYWOOD_MUSIC_URL Actions variable to the selected music bed.")
    with tempfile.TemporaryDirectory(prefix="td-hollywood-render-") as temp:
        temp = Path(temp)
        source = temp / "source.mp4"
        music = temp / "music.bin"
        download(payload["source_url"], source)
        download(music_url, music)
        outputs = []
        episodes = plan.get("episodes") or []
        if not 2 <= len(episodes) <= 3:
            raise RuntimeError("Edit plan must contain 2 or 3 episodes.")
        for index, episode in enumerate(episodes, start=1):
            final_path, seconds = render_episode(
                source, episode, index, job_id, title, music, temp,
            )
            result = cloudinary.uploader.upload_large(
                str(final_path), resource_type="video", folder="trenddock/hollywood/episodes",
                public_id=f"hollywood_{job_id}_ep{index}", chunk_size=20_000_000, overwrite=True,
            )
            video_url = result.get("secure_url")
            if not video_url:
                raise RuntimeError(f"Cloudinary returned no URL for episode {index}.")
            thumb = video_url.replace("/upload/", "/upload/so_1.0/", 1)
            thumb = re.sub(r"\.mp4(?:\?.*)?$", ".jpg", thumb)
            outputs.append({
                "episode": index,
                "rank": 4 + index,
                "title": episode.get("title") or f"{title} — Part {index}",
                "caption": episode.get("title") or f"{title} explained — Part {index}",
                "duration_seconds": seconds,
                "cloudinary_url": video_url,
                "thumbnail_url": thumb,
            })
    callback("hollywood_render_complete", {"job_id": job_id, "title": title, "outputs": outputs})
    print(f"Rendered and uploaded {len(outputs)} Hollywood explainer episodes.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "analyze":
        analyze()
    elif mode == "render":
        render()
    else:
        raise SystemExit("Usage: python hollywood_render.py [analyze|render]")
