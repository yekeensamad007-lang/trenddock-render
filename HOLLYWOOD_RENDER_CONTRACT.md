# Hollywood renderer contract

This repository handles the media-heavy analysis and render stages. It downloads
source media from Cloudinary, writes analysis manifests and finished episodes to
Cloudinary, and sends job events back to the private `trenddock` repository.
Supabase records and posting metadata stay in the private repository.

## Start analysis

Send a GitHub `repository_dispatch` event with type `hollywood_analyze` and this
`client_payload`:

```json
{
  "job_id": "stable-job-id",
  "source_url": "https://res.cloudinary.com/.../source.mp4",
  "subtitles_url": "https://res.cloudinary.com/.../source.vtt",
  "transcript_url": "https://res.cloudinary.com/.../transcript.txt",
  "title": "Movie title"
}
```

`subtitles_url` and `transcript_url` are optional. The worker uses timed
YouTube VTT subtitles when they are usable. Otherwise, it runs local Whisper to
produce accurate cue timings. If an Agent Reach transcript was supplied, its
plain text is included as a reference in the analysis manifest; it is not given
fabricated timestamps. The manifest contains the timed transcript cues,
duration, subtitle source, and detected scene-cut timestamps.

On success, the renderer dispatches `hollywood_analysis_complete` to the private
repository:

```json
{
  "job_id": "stable-job-id",
  "analysis_url": "https://res.cloudinary.com/.../analysis.json"
}
```

## Start rendering

Send `hollywood_render` with:

```json
{
  "job_id": "stable-job-id",
  "source_url": "https://res.cloudinary.com/.../source.mp4",
  "edit_plan_url": "https://res.cloudinary.com/.../edit-plan.json",
  "music_url": "https://res.cloudinary.com/.../music.mp3",
  "title": "Movie title"
}
```

The plan JSON contains 2–3 episodes. Each episode has a title and an ordered
`segments` array. Every segment has source-video `start` and `end` times in
seconds and the narration for that segment. Segment durations must add up to
6–9 minutes per episode, and timestamps must fit within the source. Narration
must contain 850–1,500 words per episode. The renderer crops to 9:16, removes
source audio, creates voiceover and synchronized captions, mixes the configured
music bed, and uploads each MP4 to Cloudinary.

On success, it dispatches `hollywood_render_complete`:

```json
{
  "job_id": "stable-job-id",
  "title": "Movie title",
  "outputs": [
    {
      "episode": 1,
      "rank": 5,
      "title": "Movie title — Part 1",
      "caption": "Movie title explained — Part 1",
      "duration_seconds": 420,
      "cloudinary_url": "https://res.cloudinary.com/.../episode.mp4",
      "thumbnail_url": "https://res.cloudinary.com/.../episode.jpg"
    }
  ]
}
```

The private workflow stores those Cloudinary URLs and posting fields in
Supabase. The renderer also dispatches `hollywood_job_failed` with `job_id`,
`stage`, and `error` when an analysis or render workflow fails.

## Renderer configuration

The `trenddock-render` GitHub Actions environment requires these secrets:

- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `CALLBACK_TOKEN`, with access to dispatch events to `trenddock`

The private controller selects a CC0 Freesound track and stages its MP3 preview
in Cloudinary. It sends the staged `music_url` to this workflow; the Freesound
API key stays in the private repository. `HOLLYWOOD_MUSIC_URL` remains an
optional fallback for manual render events. The workflow uses the Cloudinary
cloud name configured in `render.yml` and a local Piper voice model on the
Actions runner.
