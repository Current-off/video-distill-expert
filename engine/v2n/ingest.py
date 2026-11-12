"""Ingest: HLS / direct URL / local file -> media file + 16 kHz mono WAV.

Design rules, all learned from a real 11.4-hour run:

* **Atomic outputs.** Every download/extraction writes ``<name>.part<ext>`` and
  only then ``os.replace`` into place. An interrupted run can never leave a file
  that looks finished. ``.part`` must keep the extension (``audio.part.wav``),
  because ffmpeg infers the container from the suffix; ``audio.wav.part`` fails
  with ``Unable to choose an output format`` / exit -22.
* **Segment-level retry.** A 118-segment playlist with one ConnectTimeout must
  not discard the whole lesson. Only connection/timeout and 5xx are retried.
* **Resume verification.** Reusing an existing file is not ``exists()``; the WAV
  duration is compared against the playlist's ``#EXTINF`` sum first (see
  :mod:`v2n.integrity`). A truncated media file is deleted and re-downloaded.

No host, credential, cookie or catalogue is embedded here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .config import Config
from .integrity import Verdict, check_wav, playlist_duration


@dataclass(frozen=True)
class Playlist:
    segments: list[str]
    key_uri: str = ""
    iv_hex: str = ""
    media_sequence: int = 0
    duration: float = 0.0
    is_master: bool = False
    key_count: int = 1          # distinct EXT-X-KEY specs; >1 needs key rotation

    @property
    def encrypted(self) -> bool:
        return bool(self.key_uri)


@dataclass
class MediaResult:
    media: Path
    wav: Path
    expected_duration: float
    verdict: Verdict
    actions: list[str] = field(default_factory=list)
    segment_count: int = 0


def _session(cfg: Config) -> requests.Session:
    s = requests.Session()
    if not cfg.use_system_proxy:
        s.trust_env = False
    return s


def http_get(cfg: Config, url: str, *, headers: dict | None = None,
             timeout: float | None = None,
             session: requests.Session | None = None,
             stream: bool = False) -> requests.Response:
    """One GET. Pass a ``session`` to reuse a connection across many segments;
    otherwise a short-lived session is created (and never shared between
    threads, which would be unsafe for requests.Session).
    """
    kwargs = {
        "headers": {**cfg.http_headers(), **(headers or {})},
        "timeout": timeout or cfg.http_timeout,
        "proxies": cfg.proxies(),
        "stream": stream,
    }
    if session is not None:
        return session.get(url, **kwargs)
    if stream:
        # Closing the session before the body is consumed would break the
        # response. Callers doing streaming must own the session explicitly.
        raise ValueError("stream=True requires an explicit session")
    s = _session(cfg)
    try:
        return s.get(url, **kwargs)
    finally:
        s.close()


def http_get_retry(cfg: Config, url: str, *, headers: dict | None = None,
                   timeout: float | None = None, attempts: int | None = None,
                   session: requests.Session | None = None,
                   stream: bool = False) -> requests.Response:
    """GET with exponential backoff; 4xx is a hard failure (server refused)."""
    n = attempts or cfg.http_attempts
    last: Exception | None = None
    for i in range(n):
        try:
            r = http_get(cfg, url, headers=headers, timeout=timeout,
                         session=session, stream=stream)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = exc
        except requests.HTTPError as exc:
            raise RuntimeError(f"GET refused: {url} -> {exc}") from exc
        if i < n - 1:
            time.sleep(2 ** (i + 1))
    raise RuntimeError(f"GET failed after {n} attempts: {url} -> {last}")


def parse_media_playlist(text: str) -> Playlist:
    """Parse the subset of RFC 8216 needed for VOD AES-128 media playlists."""
    segs: list[str] = []
    key_uri = ""
    iv_hex = ""
    seq = 0
    duration = 0.0
    master = "#EXT-X-STREAM-INF" in text
    key_specs: set[tuple[str, str]] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-KEY"):
            spec_uri, spec_iv = "", ""
            for part in line.split(","):
                part = part.strip()
                if part.startswith("URI="):
                    spec_uri = part.split("=", 1)[1].strip().strip('"')
                    key_uri = spec_uri
                elif part.startswith("IV="):
                    spec_iv = part.split("=", 1)[1].strip().removeprefix("0x")
                    iv_hex = spec_iv
            key_specs.add((spec_uri, spec_iv))
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            m = re.match(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)", line)
            if m:
                seq = int(m.group(1))
        elif line.startswith("#EXTINF"):
            m = re.match(r"#EXTINF:\s*([0-9]+(?:\.[0-9]+)?)\s*,", line)
            if m:
                duration += float(m.group(1))
        elif line and not line.startswith("#"):
            segs.append(line)
    return Playlist(segments=segs, key_uri=key_uri, iv_hex=iv_hex,
                    media_sequence=seq, duration=duration, is_master=master,
                    key_count=len(key_specs) or 1)


def _throttle(cfg: Config, last: list[float]) -> None:
    if cfg.qps <= 0:
        return
    gap = 1.0 / cfg.qps
    wait = gap - (time.monotonic() - last[0]) if last[0] else 0.0
    if wait > 0:
        time.sleep(wait)
    last[0] = time.monotonic()


def _segment_iv(pl: Playlist, index: int) -> bytes:
    """RFC 8216 §5.2: explicit IV is fixed; otherwise use the sequence number."""
    if pl.iv_hex:
        return bytes.fromhex(pl.iv_hex)
    return (pl.media_sequence + index).to_bytes(16, "big")


def _with_query(url: str, query: str) -> str:
    """Carry a signed playlist's query onto a relative segment/key URL."""
    if not query:
        return url
    sp = urlsplit(url)
    if sp.query:                       # segment already has its own signature
        return url
    return urlunsplit((sp.scheme, sp.netloc, sp.path, query, sp.fragment))


def download_hls(cfg: Config, m3u8_url: str, out_dir: Path, *,
                 progress: bool = True) -> tuple[Path, Playlist]:
    """Download a media playlist into ``media.ts`` atomically. Returns (path, playlist).

    One Session is reused for the playlist, key and every segment: creating a
    new Session per request means a fresh TCP/TLS handshake for each of ~118
    segments, which is both slow and a reliable way to trigger timeouts.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    session = _session(cfg)
    try:
        text = http_get_retry(cfg, m3u8_url, timeout=30, session=session).text
        pl = parse_media_playlist(text)
        if pl.is_master:
            raise ValueError(
                "master playlist given; pass the variant/media playlist URL "
                "(one containing #EXTINF entries)")
        if not pl.segments:
            raise ValueError("playlist has no media segments")
        if pl.key_count > 1:
            raise ValueError(
                f"playlist has {pl.key_count} distinct EXT-X-KEY specs; key "
                "rotation is not supported — refusing rather than decrypting "
                "segments with the wrong key")
        if not pl.duration:
            pl = Playlist(pl.segments, pl.key_uri, pl.iv_hex,
                          pl.media_sequence, playlist_duration(text), False,
                          pl.key_count)

        signed_query = urlsplit(m3u8_url).query
        key = b""
        if pl.encrypted:
            try:
                from Crypto.Cipher import AES as _AES
            except ImportError as exc:
                raise RuntimeError(
                    "encrypted HLS needs the 'hls' extra: "
                    "pip install -e '.[hls]'") from exc
            key_url = _with_query(urljoin(m3u8_url, pl.key_uri), signed_query)
            key = http_get_retry(cfg, key_url, timeout=20,
                                 session=session).content[:16]
            if len(key) != 16:
                raise RuntimeError(f"AES-128 key must be 16 bytes, got {len(key)}")

        dest = out_dir / "media.ts"
        tmp = out_dir / "media.ts.part"
        last = [0.0]
        try:
            with tmp.open("wb") as out:
                for i, name in enumerate(pl.segments):
                    _throttle(cfg, last)
                    seg_url = _with_query(urljoin(m3u8_url, name), signed_query)
                    data = http_get_retry(cfg, seg_url, session=session).content
                    if pl.encrypted:
                        if len(data) % 16:
                            raise RuntimeError(
                                f"segment {i} length {len(data)} is not a multiple of 16")
                        data = _AES.new(key, _AES.MODE_CBC,
                                        _segment_iv(pl, i)).decrypt(data)
                    out.write(data)
                    if progress and ((i + 1) % 10 == 0 or i + 1 == len(pl.segments)):
                        print(f"  hls {i + 1}/{len(pl.segments)}", flush=True)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return dest, pl
    finally:
        session.close()


def download_direct(cfg: Config, url: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".mp4"
    lower = url.lower().split("?", 1)[0]
    for ext in (".mp4", ".ts", ".mkv", ".webm", ".mov"):
        if lower.endswith(ext):
            suffix = ext
            break
    dest = out_dir / f"media{suffix}"
    tmp = out_dir / f"media{suffix}.part"
    session = _session(cfg)
    try:
        # Stream to disk: loading a multi-GB lecture into memory as
        # ``resp.content`` is a reliable way to exhaust RAM.
        r = http_get_retry(cfg, url, timeout=120, session=session, stream=True)
        try:
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(65536):
                    if chunk:
                        fh.write(chunk)
        finally:
            r.close()
        if tmp.stat().st_size < 1024:
            raise RuntimeError(f"downloaded media is too small: {url}")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        session.close()
    return dest


def copy_local(src: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"media{src.suffix.lower() or '.bin'}"
    tmp = out_dir / f"{dest.name}.part"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def ffmpeg_bin() -> str:
    """Resolve ffmpeg: V2N_FFMPEG -> imageio-ffmpeg -> PATH, in that order."""
    override = os.environ.get("V2N_FFMPEG")
    if override:
        return override
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        found = shutil.which("ffmpeg")
        if found:
            return found
        raise RuntimeError(
            "ffmpeg not found; install the 'media' extra "
            "(pip install -e '.[media]') or set V2N_FFMPEG") from exc


def extract_wav(media: Path, wav: Path, cfg: Config) -> Path:
    """Extract 16 kHz mono PCM WAV atomically. Requires ffmpeg."""
    wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = wav.with_name(f"{wav.stem}.part{wav.suffix}")
    cmd = [
        ffmpeg_bin(), "-y", "-i", str(media),
        "-vn", "-ac", str(cfg.channels), "-ar", str(cfg.sample_rate),
        "-c:a", "pcm_s16le", "-f", "wav", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        os.replace(tmp, wav)
    except subprocess.CalledProcessError as exc:
        tmp.unlink(missing_ok=True)
        # capture_output swallowed ffmpeg's stderr; surface it so a broken
        # media file is diagnosable without re-running the command by hand.
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()[-800:]
        raise RuntimeError(
            f"ffmpeg failed (exit {exc.returncode}) on {media.name}: "
            f"{detail or 'no stderr captured'}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return wav


def ensure_audio(cfg: Config, out_dir: Path, source: str | Path | None,
                 *, expected_duration: float = 0.0,
                 progress: bool = True) -> MediaResult:
    """Idempotent ingest + integrity check.

    Reuse policy (the fix for the silent-truncation bug):

    1. Media exists -> do not re-download.
    2. WAV exists but fails :func:`v2n.integrity.check_wav` -> delete and re-extract.
    3. WAV still fails after re-extraction -> the media itself is truncated:
       delete it and download again.
    """
    from .integrity import Verdict as _V

    out_dir.mkdir(parents=True, exist_ok=True)
    actions: list[str] = []
    wav = out_dir / "audio.wav"
    media: Path | None = None
    pl: Playlist | None = None

    for name in ("media.ts", "media.mp4", "media.mkv", "media.webm", "media.mov"):
        p = out_dir / name
        if not p.exists():
            continue
        # A zero/near-zero file is an interrupted download, never a product.
        if p.stat().st_size >= 1024:
            media = p
            actions.append(f"reuse_media:{name}")
            break
        actions.append(f"ignore_empty_media:{name}")

    expected = expected_duration
    if media is None:
        if source is None:
            raise FileNotFoundError(f"no media in {out_dir} and no source given")
        s = str(source)
        if s.startswith("http") and ".m3u8" in s.lower():
            media, pl = download_hls(cfg, s, out_dir, progress=progress)
            actions.append("download_hls")
        elif s.startswith("http"):
            media = download_direct(cfg, s, out_dir)
            actions.append("download_direct")
        else:
            media = copy_local(Path(s), out_dir)
            actions.append("copy_local")
        if pl and pl.duration:
            expected = pl.duration

    verdict = check_wav(wav, expected, tolerance=cfg.duration_tolerance)
    if wav.exists() and not verdict.ok:
        wav.unlink()
        actions.append("drop_bad_wav")
        verdict = _V(False, expected, 0.0, -1.0, "missing")

    if not wav.exists():
        extract_wav(media, wav, cfg)
        actions.append("extract_wav")
        verdict = check_wav(wav, expected, tolerance=cfg.duration_tolerance)

    if not verdict.ok and verdict.reason == "truncated" and source is not None:
        # Media itself is short. Force one clean re-download.
        media.unlink(missing_ok=True)
        wav.unlink(missing_ok=True)
        actions.append("drop_truncated_media")
        s = str(source)
        if s.startswith("http") and ".m3u8" in s.lower():
            media, pl = download_hls(cfg, s, out_dir, progress=progress)
        elif s.startswith("http"):
            media = download_direct(cfg, s, out_dir)
        else:
            media = copy_local(Path(s), out_dir)
        actions.append("redownload")
        extract_wav(media, wav, cfg)
        actions.append("reextract_wav")
        verdict = check_wav(wav, expected, tolerance=cfg.duration_tolerance)

    return MediaResult(media=media, wav=wav, expected_duration=expected,
                       verdict=verdict, actions=actions,
                       segment_count=len(pl.segments) if pl else 0)
