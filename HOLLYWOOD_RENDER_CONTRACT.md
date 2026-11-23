# Hollywood renderer contract

This repository runs the media-heavy analysis and rendering stages. It downloads
the source bundle from a private `trenddock` Actions artifact, stores media and
manifests in Cloudinary, and sends job events back to the private repository.
Supabase records and posting metadata stay in private `trenddock`.

## Start analysis

The private repository dispatches `hollywood_analyze` with this payload:

```json
{
  "job_id": "stable-job-id",
  "title": "Movie title",
  "duration_seconds": 5400,
  "source_run_id": "github-actions-run-id",
  "source_artifact_name": "hollywood-source-job-id"
}
```

The referenced private artifact is retained for one day and contains
`source.mp4`, plus optional `source.vtt` and `transcript.txt`. The renderer
downloads it with `CALLBACK_TOKEN`, keeps the movie on the runner for analysis,
and uploads only optional subtitle/transcript files plus the analysis manifest.
It uses timed VTT subtitles when available. Otherwise it runs local Whisper to
produce cue timings. The analysis manifest contains the timed transcript cues,
duration, subtitle source, and detected scene-cut timestamps.

On success, the renderer dispatches `hollywood_analysis_complete` to private
`trenddock` with `job_id`, `analysis_url`, the original private source
artifact reference (`source_run_id` and `source_artifact_name`), optional
subtitle and transcript URLs, and `subtitles_available`.

## Start rendering

The private Judge sends `hollywood_render` with:

```json
{
  "job_id": "stable-job-id",
  "title": "Movie title",
  "source_run_id": "github-actions-run-id",
  "source_artifact_name": "hollywood-source-job-id",
  "plan_run_id": "github-actions-run-id",
  "plan_artifact_name": "hollywood-plan-job-id",
  "music_url": "https://cdn.freesound.org/... or null"
}
```

The renderer downloads both private artifacts with `CALLBACK_TOKEN`. It
assembles the story in two or three chronological arcs and lets each episode run
for the duration its story needs, within 3–10 minute safety bounds. The renderer
cuts each arc to 9:16, removes source audio, adds Piper narration and timed
captions, and mixes in CC0 music when available. If no eligible music was found,
it renders narration-only so music search cannot block video creation. It uploads
only finished episodes and the plan manifest to Cloudinary, then calls back with
the output URLs.

The source movie stays in temporary runner storage and is removed by the
workflow's unconditional cleanup step after rendering or failure. The private
source artifact expires after one day.

On failure, the renderer dispatches `hollywood_job_failed` with `job_id`,
`stage`, and a concise error summary.

## Renderer configuration

The `trenddock-render` GitHub Actions repository requires these secrets:

- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `CALLBACK_TOKEN`, with access to dispatch events to private `trenddock` and
  read its Actions artifacts (`Actions: read`).

The Cloudinary cloud name is `dnzxeped5`. `HOLLYWOOD_MUSIC_URL` remains an
optional fallback for manual render events. The Freesound API key stays in the
private repository.
