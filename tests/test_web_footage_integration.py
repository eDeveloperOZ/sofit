"""Real ffmpeg integration, mocked discovery/network/vision. No external service."""

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sofit import footage as f, generate, render, storyboard
from sofit.transcribe import Segment, Word

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="integration needs ffmpeg + ffprobe")


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, timeout=120).stdout


@pytest.mark.parametrize("provider_kind", ["fixture", "youtube", "direct"])
def test_transcript_to_selected_web_cutaway_and_real_render(monkeypatch, tmp_path, provider_kind):
    Image = pytest.importorskip("PIL.Image")
    original, footage = tmp_path / "episode.mp4", tmp_path / "web.mp4"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=180x320:r=30:d=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(original))
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=320x240:r=30:d=24",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=24",
        "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=green:t=fill:enable='between(t,8,16)'",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(footage))
    words = [Word(i / 2, (i + 1) / 2, ["בדיקת", "כתוביות", "רציפות"][i % 3]) for i in range(24)]
    transcript = [Segment(0, 0, 12, " ".join(w.text for w in words), words)]
    clip = generate.clip_spec(generate.Quote(0, 12, "בדיקה"), transcript, "clip-1")
    clip["focus"] = 0.5
    doc = {"schema_version": 1, "source": {"video": str(original)}, "clips": [clip]}
    queries = []
    class Provider:
        name = "fixture"
        def search(self, query, limit=8):
            queries.append(query)
            return [f.Candidate(self.name, "https://example.org/robot", "https://example.org/robot.mp4",
                                "Unitree robot running", 24, 320, 240, license="Public domain",
                                creator="Fixture", attribution_required=False)]
        def subtitles(self, candidate):
            return [f.Cue(10, 14, "Unitree robot running")]
    class Judge:
        # The green segment represents the visible action in this synthetic fixture.
        cache_key = "fixture-green-v1"
        def score(self, frames, intent):
            scores = []
            for frame in frames:
                with Image.open(frame.path) as im:
                    r, g, b = im.getpixel((im.width // 2, im.height // 2))
                scores.append((0.99 if g > r + 50 else 0.1, "visible green action"))
            return scores
    downloads = []
    def download(url, timeout=30):
        downloads.append(url)
        data = footage.read_bytes()
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data))}
        return response
    monkeypatch.setattr(f, "open_url", download)
    def plan(system, user, validate, **kwargs):
        assert "כתוביות" in user
        return validate({"cutaways": [{"span": 0, "start": 4, "end": 8, "source": "web",
                         "intent": "Unitree robot running", "query": "Unitree robot running",
                         "context": "A robot demonstration"}]})
    monkeypatch.setattr(storyboard, "call_claude_json", plan)
    spec = tmp_path / "episode.clips.json"
    cache = f.FootageCache(tmp_path / "cache")
    providers, urls = [Provider()], ()
    if provider_kind == "youtube":
        from sofit import footage_youtube as yt
        def extract(target, flat=False):
            if flat:
                queries.append(target)
                return {"entries": [{"url": "https://www.youtube.com/watch?v=abcdefghijk",
                                     "title": "Unitree robot running", "duration": 24}]}
            return {"webpage_url": target, "title": "Unitree robot running", "duration": 24,
                    "url": "https://r1.googlevideo.com/videoplayback", "protocol": "https",
                    "format_id": "136", "width": 320, "height": 240, "channel": "Fixture",
                    "license": "Public domain", "upload_date": "20260918"}
        monkeypatch.setattr(yt, "_extract", extract)
        providers = [yt.YouTubeProvider()]
    elif provider_kind == "direct":
        urls = ("https://example.org/123.mp4",)
    assert storyboard.add_web_cutaways(doc, str(spec), providers=providers, cache=cache,
                                       judge=Judge(), safe_only=provider_kind == "fixture",
                                       source_urls=urls) == 1
    stored = json.loads(spec.read_text())
    cutaway = stored["clips"][0]["cutaways"][0]
    assert 8 <= cutaway["source"]["selection"]["start"] < cutaway["source"]["selection"]["end"] <= 16
    assert stored["clips"][0]["words"] == clip["words"]
    # A correction/rerender needs neither network nor the planning/model backend.
    assert storyboard.add_web_cutaways(stored, str(spec), providers=providers, cache=cache,
                                       judge=Judge(), safe_only=provider_kind == "fixture") == 0
    assert len(downloads) == 1
    assert len(queries) == (0 if provider_kind == "direct" else 1)
    monkeypatch.setattr(render, "_target_resolution", lambda *a: (180, 320))
    output = Path(render.render_clips(str(original), stored["clips"], str(tmp_path / "out"), hook_card=False)[0])
    baseline_clip = {**clip, "cutaways": []}
    baseline = Path(render.render_clips(str(original), [baseline_clip], str(tmp_path / "baseline"), hook_card=False)[0])
    probe = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)))
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["pix_fmt"]) == (180, 320, "yuv420p")
    assert abs(float(probe["format"]["duration"]) - 12) < 0.15
    audio = lambda p: run("ffmpeg", "-v", "error", "-i", str(p), "-map", "0:a:0", "-f", "s16le", "-")
    assert audio(output) == audio(baseline)  # external 880Hz audio never enters the mix
    for at, green in [(1, False), (6, True), (10, False)]:
        image = Image.open(io.BytesIO(run("ffmpeg", "-v", "error", "-ss", str(at), "-i", str(output),
                                         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-")))
        r, g, b = image.getpixel((5, 160))
        assert (g > b + 50) if green else (b > g + 50)
        # White/yellow caption pixels remain visible before, during and after.
        pixels = [image.getpixel((x, y)) for x in range(180) for y in range(170, 290)]
        assert sum(r > 170 and g > 170 for r, g, b in pixels) > 10
    credit = json.loads(output.with_suffix(".sources.json").read_text())["sources"][0]
    assert credit["creator"] == ("" if provider_kind == "direct" else "Fixture")
    assert credit["license"] == ("unknown" if provider_kind == "direct" else "Public domain")
    assert credit["selection"] == cutaway["source"]["selection"]
