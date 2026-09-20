"""Web footage contracts, inexpensive search ranking, and bounded local caching.

Providers return metadata, never renderer instructions. Only downloaded, probed
local video reaches ffmpeg; the final asset uses the existing cutaway contract.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


class FootageError(RuntimeError):
    pass


@dataclass(frozen=True)
class VisualIntent:
    intent: str
    query: str
    duration: float
    context: str = ""

    def __post_init__(self):
        if not self.intent.strip() or not self.query.strip():
            raise ValueError("footage needs a visual intent and search query")
        if not math.isfinite(self.duration) or not 2 <= self.duration <= 8:
            raise ValueError("footage duration must be between 2 and 8 seconds")


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Candidate:
    provider: str
    source_url: str
    media_url: str
    title: str
    duration: float
    width: int
    height: int
    description: str = ""
    tags: tuple[str, ...] = ()
    creator: str = ""
    license: str = "unknown"
    license_url: str = ""
    attribution: str = ""
    attribution_required: bool | None = None
    restrictions: str = ""
    original_media_url: str = ""
    size: int | None = None
    subtitle_urls: tuple[str, ...] = ()
    cues: tuple[Cue, ...] = ()


class FootageProvider(Protocol):
    """A provider owns discovery and optional timed text, not downloading/rendering."""

    name: str

    def search(self, query: str, limit: int = 8) -> list[Candidate]: ...

    def subtitles(self, candidate: Candidate) -> list[Cue]: ...


@dataclass(frozen=True)
class Limits:
    max_bytes: int = 256 * 1024 * 1024
    max_duration: float = 600
    timeout: float = 30
    download_seconds: float = 180


@dataclass(frozen=True)
class VideoInfo:
    duration: float
    width: int
    height: int


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sofit" / "footage"


def canonical_url(url: str) -> str:
    """Keep query parameters (including signatures); fragments never identify bytes."""
    p = urllib.parse.urlsplit(url)
    if p.scheme.lower() != "https" or not p.hostname or p.username or p.password:
        raise FootageError("footage requires an HTTPS URL without credentials")
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    return urllib.parse.urlunsplit(("https", host + (f":{port}" if port and port != 443 else ""),
                                    p.path or "/", p.query, ""))


def _public_url(url: str) -> str:
    url = canonical_url(url)
    p = urllib.parse.urlsplit(url)
    addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FootageError("footage URL resolves to a non-public address")
    return url


def _connect_public(address, timeout=30, source_address=None, **kwargs):
    """Connect to the validated numeric address, closing the DNS-rebinding gap."""
    host, port = address
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FootageError("footage URL resolves to a non-public address")
    last = None
    for family, kind, proto, _, destination in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(destination)
            return sock
        except OSError as e:
            last = e
            sock.close()
    raise last or FootageError("no public address available")


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect_public


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPSConnection, req, context=self._context)


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _public_url(newurl))


def open_url(url: str, timeout: float = 30):
    """Validate initial and redirected destinations; never accept local/file URLs."""
    req = urllib.request.Request(_public_url(url), headers={
        "User-Agent": "Sofit/0.7 (https://github.com/navotvolkgroundup/sofit)",
        "Accept-Encoding": "identity",
    })
    # Direct HTTPS preserves the validated destination. Environment proxies
    # could resolve/connect elsewhere; this downloader intentionally ignores them.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicRedirect(),
                                       _PublicHTTPSHandler()).open(req, timeout=timeout)


def fetch_bytes(url: str, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    deadline = time.monotonic() + 60
    data = bytearray()
    with open_url(url) as response:
        for chunk in iter(lambda: response.read1(64 * 1024), b""):
            data.extend(chunk)
            if len(data) > max_bytes or time.monotonic() > deadline:
                raise FootageError("metadata response exceeded size/time limit")
    return bytes(data)


def _tokens(text: str) -> set[str]:
    ignored = {"a", "an", "the", "of", "on", "in", "at", "and", "with", "to", "for",
               "real", "footage", "video", "show", "showing", "file", "webm", "mp4"}
    return {t for t in re.findall(r"\w+", text.casefold()) if len(t) > 1 and t not in ignored}


def relevance(query: str, text: str) -> float:
    terms = _tokens(query)
    return len(terms & _tokens(text)) / len(terms) if terms else 0.0


def license_allowed(candidate: Candidate, safe_only: bool = False) -> bool:
    """Default records supplied rights without gating. Safe-only is an allowlist.

    This is a metadata filter, not legal clearance. Share-alike/NC/ND/custom
    licenses deliberately require the unrestricted mode and editorial review.
    """
    if not safe_only:
        return True
    if candidate.restrictions.strip():
        return False
    name = candidate.license.strip().lower()
    if name in {"public domain", "pd", "cc0", "cc0 1.0"}:
        return candidate.attribution_required is False
    p = urllib.parse.urlsplit(candidate.license_url)
    return (p.scheme == "https" and p.hostname in {"creativecommons.org", "www.creativecommons.org"}
            and bool(re.fullmatch(r"/licenses/by/(1\.0|2\.0|2\.5|3\.0|4\.0)/?", p.path))
            and bool(re.fullmatch(r"cc by (1\.0|2\.0|2\.5|3\.0|4\.0)", name))
            and candidate.attribution_required is True
            and bool(candidate.creator.strip()) and bool(candidate.attribution.strip()))


def rank_candidates(candidates: list[Candidate], intent: VisualIntent,
                    safe_only: bool = False, limits: Limits = Limits()) -> list[Candidate]:
    """Semantic text overlap first; resolution/length only break plausible ties."""
    ranked = []
    seen = set()
    for c in candidates:
        try:
            url = canonical_url(c.media_url)
        except (FootageError, ValueError):
            continue
        if (url in seen or not math.isfinite(c.duration)
                or not intent.duration <= c.duration <= limits.max_duration
                or min(c.width, c.height) < 240
                or (c.size is not None and not 0 < c.size <= limits.max_bytes)
                or not license_allowed(c, safe_only)):
            continue
        title = relevance(intent.query, c.title)
        body = relevance(intent.query, c.description + " " + " ".join(c.tags))
        cues = relevance(intent.query, " ".join(cue.text for cue in c.cues))
        semantic = max(title, 0.85 * body, 0.9 * cues)
        if semantic < 0.35:
            continue
        seen.add(url)
        quality = min(c.width * c.height / (1920 * 1080), 1)
        score = semantic + 0.05 * quality + 0.03 * min(intent.duration / c.duration, 1)
        ranked.append((score, c))
    return [c for _, c in sorted(ranked, key=lambda pair: (-pair[0], pair[1].source_url))]


def probe_video(path: Path, limits: Limits = Limits()) -> VideoInfo:
    """ffprobe cannot follow network references hidden inside downloaded media."""
    if not path.is_file() or not 0 < path.stat().st_size <= limits.max_bytes:
        raise FootageError("missing, empty, or oversized footage")
    try:
        p = subprocess.run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "mov,matroska,webm,ogg,avi",
            "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height,duration",
            "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        data = json.loads(p.stdout)
        stream = next(s for s in data.get("streams", []) if s.get("codec_type") == "video")
        duration = float(stream.get("duration") or data["format"]["duration"])
        width, height = int(stream["width"]), int(stream["height"])
        formats = set(data["format"]["format_name"].split(","))
        if (p.returncode or not stream.get("codec_name") or not math.isfinite(duration)
                or not 0 < duration <= limits.max_duration
                or not 0 < width <= 7680 or not 0 < height <= 7680
                or not formats & {"mov", "mp4", "matroska", "webm", "ogg", "avi"}):
            raise ValueError("unsupported or out-of-bounds video")
        return VideoInfo(duration, width, height)
    except (KeyError, ValueError, StopIteration, subprocess.SubprocessError) as e:
        raise FootageError("invalid or unsupported video") from e


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, data: dict) -> None:
    """Replace atomically; failed writes never destroy the previous spec/metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class FootageCache:
    def __init__(self, root: Path | None = None, limits: Limits = Limits()):
        self.root = Path(root) if root is not None else cache_dir()
        self.limits = limits

    def retrieve(self, candidate: Candidate) -> tuple[Path, dict]:
        url = canonical_url(candidate.media_url)
        key = hashlib.sha256(url.encode()).hexdigest()
        directory = self.root / key
        directory.mkdir(parents=True, exist_ok=True)
        media, meta = directory / "source.media", directory / "source.json"
        if media.exists() and meta.exists():
            try:
                saved = json.loads(meta.read_text(encoding="utf-8"))
                if (saved["url"] == url and saved["size"] == media.stat().st_size
                        and saved["sha256"] == _hash(media)):
                    probe_video(media, self.limits)
                    return media, saved
            except (OSError, ValueError, KeyError, FootageError):
                pass  # recover an interrupted/corrupt cache, without reusing its bytes
        if candidate.duration > self.limits.max_duration:
            raise FootageError("source exceeds duration limit")
        if candidate.size is not None and candidate.size > self.limits.max_bytes:
            raise FootageError("source exceeds file size limit")
        fd, temp_name = tempfile.mkstemp(dir=directory, suffix=".part")
        temp = Path(temp_name)
        try:
            deadline = time.monotonic() + self.limits.download_seconds
            with os.fdopen(fd, "wb") as f, open_url(url, self.limits.timeout) as response:
                if getattr(response, "status", 200) != 200:
                    raise FootageError("unexpected partial footage response")
                expected = response.headers.get("Content-Length")
                expected = int(expected) if expected is not None else None
                if expected is not None and not 0 < expected <= self.limits.max_bytes:
                    raise FootageError("source exceeds file size limit")
                size = 0
                for chunk in iter(lambda: response.read1(64 * 1024), b""):
                    size += len(chunk)
                    if size > self.limits.max_bytes or time.monotonic() > deadline:
                        raise FootageError("footage download exceeded size/time limit")
                    f.write(chunk)
                if expected is not None and size != expected:
                    raise FootageError("partial footage download")
            info = probe_video(temp, self.limits)
            saved = {"url": url, "size": size, "sha256": _hash(temp),
                     "retrieved_at": datetime.now(timezone.utc).isoformat(),
                     "candidate": asdict(candidate), "video": asdict(info)}
            os.replace(temp, media)
            write_json(meta, saved)
            return media, saved
        finally:
            temp.unlink(missing_ok=True)
