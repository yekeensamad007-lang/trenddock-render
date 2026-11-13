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
downloads it with `CALLBACK_TOKEN`, stages the source and optional text assets
in Cloudinary, and uses timed VTT subtitles when available. Otherwise it runs
local Whisper to produce cue timings. The analysis manifest contains the timed
transcript cues, duration, subtitle source, and detected scene-cut timestamps.

On success, the renderer dispatches `hollywood_analysis_complete` to private
`trenddock` with `job_id`, `analysis_url`, `source_url`, optional subtitle
and transcript URLs, and `subtitles_available`.

## Start rendering

The private Judge sends `hollywood_render` with:

```json
{
  "job_id": "stable-job-id",
  "source_url": "https://res.cloudinary.com/.../source.mp4",
  "title": "Movie title",
  "source_run_id": "github-actions-run-id",
  "plan_artifact_name": "hollywood-plan-job-id",
  "music_url": "https://cdn.freesound.org/..."
}
```

The renderer downloads the private edit-plan artifact with `CALLBACK_TOKEN`, then stages the plan and Freesound preview in Cloudinary. Each episode is cut to 9:16, source audio is removed, Piper narration and timed
captions are added, and the music bed is mixed in. The renderer uploads each
episode and calls back with the output URLs and the Cloudinary edit-plan URL.

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
