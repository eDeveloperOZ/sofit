# Footage performance validation

Start with a copied spec containing 2–3 representative clips: one with no previous
coverage, one partially covered, and one sharing a source. Keep original specs
and outputs intact. A useful baseline needs the same media, plans, quality gate,
model and source availability; total duration alone cannot identify a bottleneck.

```bash
uv run sofit --render-from subset.clips.json --render-clips subset-out \
  --web-cutaways --footage-coverage 90 --titler claude-cli \
  --footage-workers 2 --progress --progress-file subset-out/progress.jsonl
```

The progress option writes to stderr, leaving stdout available to callers.
`--progress json` also wraps ordinary diagnostic lines as JSON log events.
`--progress-file` appends JSONL; use a new filename to separate runs. Heartbeats
appear every five seconds during long calls. Events include clip/source/beat
counts, downloads, rendered clips, cache hits, Claude calls and elapsed time.
No ETA is emitted: source/model latency is too variable for an uncalibrated estimate.

The output directory's `footage-metrics.json` contains totals. The JSONL also keeps
per-clip completion latency and coverage, as well as rejection/failure events:

| Metric | Meaning |
| --- | --- |
| `seconds.search`, `resolve`, `metadata` | Provider search and source metadata work |
| `seconds.download`, `counts.bytes_downloaded` | Transfer/probe time and received bytes, including failed transfers |
| `counts.cache_hit`, `cache_miss` | Reuse across media, metadata, evidence, excerpts and in-run futures; not unique source counts |
| `seconds.frames`, `counts.frames_extracted` | Decoder/index work and produced frames |
| `seconds.judge` | Visual batches, including waiting for the model semaphore |
| `counts.claude_calls`, `seconds.claude` | Actual API/CLI transport attempts and time, including retries/planning |
| `counts.source_analyses`, `repeated_source_analyses` | Uncached evidence passes by source content hash |
| `seconds.beat_selection`, `normalize`, `ffmpeg`, `render` | Selection, excerpt normalization and final composition |
| `clip_seconds` on `rendered` events | Time since previous completed clip, including preparation before the first clip |
| `elapsed_seconds` | Batch wall time |

Concurrent and nested stage times overlap; **do not sum them** into total wall
time. Additional analysis of a genuinely different action is legitimate. A quota
failure with zero coverage measures failure handling, not successful visual quality.
Inspect `.coverage.json`, `.sources.json`, actual frames and continuous source audio
before expanding the subset. Never fill gaps with unrelated or repeated excerpts
just to reach the target.

## Reproducible offline comparison

This fixture benchmark is network-free and needs the render/dev extras plus
FFmpeg. It creates a synthetic video with separated relevant/irrelevant windows,
audio-only clips, a replayed download and a deterministic pixel-based judge.
It compares the legacy per-beat selector with the source session using real
normalization/composition. It does **not** estimate live Claude or YouTube latency.

```bash
uv sync --extra dev --extra render
SOFIT_BRAND=off uv run python scripts/benchmark_footage.py \
  --out /tmp/sofit-footage-benchmark
```

The output directory must be empty. Each engine starts with a cold cache, then
reruns with the cache warm and automatic cutaways removed from the spec to exercise
selection again. Results include wall time, downloads/bytes, source analyses,
frame passes, fixture judge batches, cache hits and coverage for all three clips.
`results.json`, stage reports, JSONL and rendered videos stay outside the repository.
A fully resolved saved spec is faster still: it bypasses selection entirely.

Cold media/index/evidence work scales with distinct sources and actions. Warm
identical actions need no additional model calls; source metadata availability,
new actions, source decoding and final encoding remain potential bottlenecks.
Automatic planning is still per clip. Real-source live cold/warm benchmarks must
be reported separately from this deterministic regression fixture.
