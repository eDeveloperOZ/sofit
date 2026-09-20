# Real web footage cutaways

The feature extends `storyboard.plan_cutaways` and the existing dictionary-based
clip spec, without changing `schema_version: 1` or introducing a second renderer.

```
clip words → one visual plan → provider search → metadata/rights ranking
  → bounded cached download → subtitles/coarse frames → fine visual windows
  → silent local video + provenance → existing cutaway/captions/audio renderer
```

## Contracts

- `footage.VisualIntent`: English intent/query, desired duration, spoken context.
- `footage.FootageProvider`: `name`, `search(query, limit)` and
  `subtitles(candidate)`. An implementation without timed text returns `[]`.
- `footage.Candidate`: provider-independent source/media URLs, dimensions,
  duration, optional size/cues, descriptive text and rights metadata.
- `footage_selection.FrameJudge`: a batch `score(frames, intent)` and a stable
  `cache_key`. The built-in judge reuses Sofit's Claude transport and model
  override; a library caller can inject another judge without changing rendering.
- `SelectedSegment`: source URL, start/end, confidence and visible-evidence reason.
- Resolved cutaway: existing `span`, `start`, `end`, plus a local `video`, optional
  `fit`, `asset_sha256`, and `source` metadata. An `image` is no longer required
  alongside a video. A still-only/generated cutaway retains its existing behavior.

`visual_plan` times are relative to each **kept span**, exactly like captions and
existing cutaways. The source excerpt's times are separate, in
`cutaways[].source.selection`; they never shift the podcast timeline. For example:

```json
{
  "visual_plan": [{
    "span": 0, "start": 4, "end": 8, "duration": 4,
    "source": "web", "intent": "a Unitree humanoid robot running",
    "query": "Unitree humanoid robot running",
    "context": "The speaker describes a running demonstration.",
    "prompt": "A humanoid robot running on a track, illustrated"
  }]
}
```

A saved plan can also be authored directly through the CLI's clips JSON interface.
The planner omits beats best served by the original recording, reserves the hook
and closing seconds, rejects overlaps and caps plans at two beats. Editorial
cutaways already occupying a beat take precedence. `source: "generated"` beats
are rendered only when the generated fallback is explicitly enabled.

## Discovery and selection

The MVP provider uses Commons' public `generator=search`, `filetype:video` and
`videoinfo` API. It normalizes HTML metadata into text and prefers a 360–1080p
transcode near 720p, avoiding multi-gigabyte originals. Only Commons upload hosts
are accepted by this provider. Commons timed-text tracks are preferred in English.
No YouTube scraping, stock API, yt-dlp, or new dependency is involved.

Catalog search is strict: the planner keeps queries concise (usually 2–4 words),
preserving named subjects while keeping visual details in `intent`. For example,
use `rocket launch` to search and describe the engine flame in the intent. Saved
plans are editable when a search is too specific; an empty result keeps the
original footage.

Ranking uses English token overlap across title, description/tags and supplied
cues; dimensions and duration only break relevant ties. Rights filtering happens
before any download. At most eight results per provider and two candidates per
beat proceed to download and visual inspection.

Up to three matching subtitle windows produce at most 12 coarse frames. Without
matching subtitles, 12 evenly spaced frames cover the source. The strongest
visible match gets a second batch of at most 25 frames over twice the requested
excerpt duration. A window must have at least three observations, all scoring
at least 0.75, including end-boundary evidence. Low-scoring samples cannot be
bridged. Exact boundaries stay inside the actual probed source duration.

This costs at most two visual batches per candidate (each has the shared JSON
helper's single retry), not one model call per video frame. Sparse sampling can
miss brief actions, and models can misidentify subjects; the confidence score is
a judgment, not a calibrated probability. Inspect the resulting cutaway before
publication. Unknown/missing subtitles never trigger full-source transcription.

## Cache, media and failure boundaries

The cache is under the same XDG convention as transcription, in `sofit/footage`.
A canonical HTTPS URL keys the original; its content hash, candidate metadata,
intent and judge version/model key each selected excerpt. Metadata sits alongside
media. Atomic writes and unique temporary files prevent partial files becoming
cache hits; size/hash/probe checks recover missing or corrupt entries. Concurrent
cold requests may download the same source, but publish complete files atomically.
There is no automatic eviction; delete this cache directory to reclaim space.

Defaults: 256 MiB, 600 source seconds, 30-second socket/probe timeout,
180-second download budget, 120-second normalization timeout, 7680px maximum
source dimensions. The library's `FootageCache(..., limits=Limits(...))` can set
stricter download limits. Only direct public HTTPS is fetched: credentials in
URLs, local destinations and private-address redirects are rejected; connections
pin validated IPs while retaining TLS hostname verification. Environment HTTP
proxies are not used. Media subprocesses accept only local file/pipe protocols
and video container formats, never remote playlists.

Normalization decodes the selected excerpt into silent H.264/yuv420p, square
pixels and 30fps without stretching or upscaling. Composition preserves its
aspect, using containment by default or `fit: "cover"`. Captions, logo and original
audio use the existing pipeline. Decode/composition errors retry with the original
recording; source errors still surface if that retry fails.

Search/vision/network/metadata failures are isolated per candidate and clip. No
confident asset means generated art if enabled and available, otherwise the
original. Existing specs work offline; enabling web cutaways reuses successful
plans/assets and can recover deleted assets. Safe-only also rechecks saved source
metadata, but does not refresh upstream license pages for an offline rerender.

## Rights and provenance

The default retains provider-supplied metadata without a license gate. Safe-only
is a deliberately narrow allowlist: public domain or CC0 explicitly reporting no
attribution requirement; or CC BY with a recognized Creative Commons URL, named
creator and attribution. It excludes unknown/custom/NC/ND/ShareAlike licenses and
reported restrictions. Missing metadata remains unknown, never fabricated.

The spec, cache and rendered `<clip-id>.sources.json` retain source URL, provider,
title, creator, license/URL, attribution text and requirement, restrictions,
retrieval time, original media URL, query/context, selected timestamps and the
modification note. The sidecar records only assets used in successful composition;
it is not an automatic attribution publication or rights-clearance mechanism.

Provider reference: [Commons MediaWiki API](https://commons.wikimedia.org/wiki/Commons:API/MediaWiki).
