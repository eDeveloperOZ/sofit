# Real web footage cutaways

The feature extends `storyboard.plan_cutaways` and the existing dictionary-based
clip spec, without changing `schema_version: 1` or introducing a second renderer.

```
clip words → one visual plan → provider search → metadata/rights ranking
  → bounded cached download → subtitles/coarse frames → fine visual windows
  → silent local video + provenance → existing cutaway/captions/audio renderer
```

## Contracts

- `footage.VisualIntent`: English intent/query, desired duration, spoken context,
  optional explicit source URLs, upload-date cutoff, recent-upload preference,
  required subject/version terms and preferred publisher names/handles.
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
Sparse planning omits beats best served by the recording and reserves the hook
and closing seconds. Audio-only CLI inputs default to 85% coverage; an explicit
`--footage-coverage 0..100` overrides the target. Dense planning can cover the
opening/closing too (titles/captions are composed above it), with a duration-based
beat budget up to 64. Manual plans also accept up to 64 beats and 2–30s windows.
Plans reject overlaps and out-of-span times. Editorial
cutaways already occupying a beat take precedence. `source: "generated"` beats
are rendered only when the generated fallback is explicitly enabled.

## Discovery and selection

The MVP provider uses Commons' public `generator=search`, `filetype:video` and
`videoinfo` API. It normalizes HTML metadata into text and prefers a 360–1080p
transcode near 720p, avoiding multi-gigabyte originals. Only Commons upload hosts
are accepted by this provider. Commons timed-text tracks are preferred in English.
Commons has no optional dependency or API key.

The YouTube provider uses the existing `youtube` extra (`yt-dlp[default]`) for
public search and individual-video metadata. It resolves at most four of eight
search hits, rejects live/private/age-restricted/DRM results and selects a direct
HTTPS video stream up to 1080p. It never downloads through yt-dlp: the same bounded
Sofit downloader validates Google video URLs and media. HLS/DASH-only sources are
skipped. Metadata extraction uses yt-dlp's own networking in a subprocess with a
90s timeout, bounded JSON output and socket/retry limits; configs, plugins,
browser cookies and remote component downloads are disabled. Deno or Node 22+
is needed for YouTube's player challenges; packaged EJS scripts come with the extra.

YouTube removed sort-by-upload-date. Recent intents instead use an upload window
(last month by default; week/month/year for an explicit cutoff) and then rank by
relevance plus a small upload-age bonus. `published_after` rejects unknown/older
dates across all providers. Upload dates are not event dates. Title/channel,
upload date and reported license persist as provenance without fabricated rights.
YouTube metadata often lacks enough information for the safe-only allowlist.

Explicit `source_urls` replace discovery: YouTube URLs resolve through that
provider, and direct HTTPS files are bounded-downloaded/probed by `DirectProvider`.
Opaque filenames do not need a keyword match, but all links still pass visual
selection. Direct files have unknown rights/date metadata; safe-only or an explicit
date cutoff skips them before download. Generic HTML pages/playlists are unsupported.
At most eight explicit links are accepted. Direct-file metadata probing downloads
each candidate under the same per-file bounds; only two ranked candidates reach
visual inspection. Prefer a short list when the source files are large.

Planning receives clip words, its hook and `visual_context`: up to 180s before
and 30s after the clip from the existing transcript, capped at 16,000 characters.
This preserves topic introductions lost in short edits. New clip specs include
it; the CLI recovers it from the transcript cache for old specs when possible.
User-supplied clip/document context takes precedence. No render triggers transcription.

Queries preserve company/product/version, event and demonstration, rather than
reducing to generic categories. `required_terms` gate publisher metadata before
download (all subject words, intact decimal versions). A title's omitted company
may be supplied by its channel name. `preferred_channels` boosts exact publisher
name/handle matches before and after full metadata extraction; a video's title
saying 'official' is not proof. If no eligible result or no preferred publisher
is found, a single retry searches
the required subject/version without action adjectives. It never relaxes identity.
Explicit bare-file links have no subject metadata, so rely on their editorial
selection and the same visual evidence gate. Verified `source_urls` remain the
strongest way to pin research-backed sources. Search/link discovery is cached
within a run, including repeated publisher queries across beats.

Ranking uses English token overlap across title, description/tags and supplied
cues; dimensions and duration only break relevant ties. Rights filtering happens
before any download. At most eight results per provider and two candidates per
beat proceed to download and visual inspection.

Up to three matching subtitle windows produce at most 12 coarse frames. Without
matching subtitles, 12 evenly spaced frames cover the source. The strongest
visible match gets fine review over twice the requested excerpt duration: up to
25 frames for shots <=8s, up to 61 for longer shots (roughly one-second spacing
even at 30s). Each model batch is limited to 25 images. A window must have at least three observations, all scoring
at least 0.75, including end-boundary evidence. Low-scoring samples cannot be
bridged. Exact boundaries stay inside the actual probed source duration.

This costs at most four visual batches per candidate (each has the shared JSON
helper's single retry), not one model call per video frame. Sparse sampling can
miss brief actions, and models can misidentify subjects; the confidence score is
a judgment, not a calibrated probability. Inspect the resulting cutaway before
publication. Unknown/missing subtitles never trigger full-source transcription.

## Cache, media and failure boundaries

The cache is under the same XDG convention as transcription, in `sofit/footage`.
A canonical HTTPS URL keys the original; YouTube uses canonical video+format
identity so rotating signed URLs do not redownload cached bytes. Its content hash, candidate metadata,
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

`footage_coverage` stores each clip's target. `footage_coverage_report` measures the
union of usable video windows across kept spans, excluding stills and missing
assets, and lists gaps. Planning retries an under-target plan once and retains a
useful partial plan if the target still cannot be met. No confidence thresholds
are relaxed. Rendering recalculates coverage from successfully composed windows
and saves `<clip-id>.coverage.json`, so composition fallback cannot claim coverage
it didn't deliver. Old automatic assets with orphaned `plan_id`s are removed when
a plan is edited; independent editorial cutaways are preserved.

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
YouTube references: [yt-dlp](https://github.com/yt-dlp/yt-dlp),
[JavaScript setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS),
[removal of date sorting](https://github.com/yt-dlp/yt-dlp/pull/15959).
