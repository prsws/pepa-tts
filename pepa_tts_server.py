#!/usr/bin/env python3
"""pepa_tts_server.py - Pepa text-to-speech endpoint (Mac Studio, port 8100).

Contract
  POST /synthesize   multipart/form-data
      file    the .md narration text (required; the filename sets the output name)
      model   engine name; only "piper" for now (required)
      voice   Piper voice name, e.g. en_US-lessac-medium (required)
      format  "wav" (default) or "bwf"
    200  audio/wav   name.wav  or  name-bwf.wav  (Content-Disposition carries the name)
    4xx/5xx          application/json error, no audio

  GET /voices         {"piper": ["en_US-lessac-medium", ...]}
  GET /health         {"status": "ok", ...}

Design rules (settled 2026-10-19/20)
  - On demand: the listener is resident and idle; Piper runs as a child process
    for one request and exits, so no model stays loaded between jobs.
  - One job at a time; a second request while busy gets 503 "busy".
  - Lint before speech: strip **bold**. Quote marks are left alone (models use
    them for emphasis). Rules live in LINT_RULES and can be extended.
  - Output at the voice's native sample rate; no resampling yet.
  - Single-speaker Piper voices only.
  - BWF (opt-in): bext + LIST/INFO chunks embedded; transcript chunk is a stub.

Standard library only (plus piper-tts in the same Python environment).
"""
import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICE = "pepa-tts"
SERVICE_VERSION = "0.1.0"
MAX_UPLOAD_BYTES = 1_000_000          # narration drafts are a few KB
SYNTH_TIMEOUT_S = 600

# Command that runs Piper. Overridable for testing (PEPA_TTS_PIPER_CMD="python3 fake.py").
PIPER_CMD = os.environ.get("PEPA_TTS_PIPER_CMD", f"{sys.executable} -m piper").split()

JOB_LOCK = threading.Lock()
VOICES_DIR: Path = Path(".")


# ---------------------------------------------------------------- linter ----
# Each rule: (name, compiled regex, replacement). Add rules here.
LINT_RULES = [
    ("strip_bold", re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
]


def lint(text):
    """Return (clean_text, [names of rules that changed something])."""
    fired = []
    for name, rx, repl in LINT_RULES:
        new = rx.sub(repl, text)
        if new != text:
            fired.append(name)
            text = new
    return text.strip(), fired


# ---------------------------------------------------------------- voices ----
def list_voices():
    """Single-speaker Piper voices found in VOICES_DIR: {name: config}."""
    found = {}
    for onnx in sorted(VOICES_DIR.glob("*.onnx")):
        cfg_path = onnx.with_name(onnx.name + ".json")
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if int(cfg.get("num_speakers", 1)) != 1:
            continue                      # multi-speaker voices not offered
        found[onnx.stem] = cfg
    return found


def piper_version():
    try:
        return importlib.metadata.version("piper-tts")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


# ------------------------------------------------------------------ IDs -----
def uuid7():
    """UUIDv7 string (time-ordered), stdlib only."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    val = (ms & ((1 << 48) - 1)) << 80
    val |= 0x7 << 76
    val |= ((rand >> 62) & 0xFFF) << 64
    val |= 0b10 << 62
    val |= rand & ((1 << 62) - 1)
    h = f"{val:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


# ------------------------------------------------------------------ BWF -----
def _chunk(cid, data):
    pad = b"\x00" if len(data) % 2 else b""
    return cid + struct.pack("<I", len(data)) + data + pad


def _ascii(s, size=None):
    b = s.encode("ascii", "replace")
    return b[:size] if size else b


def bext_chunk(description, originator, reference, when, coding_history):
    """EBU Tech 3285 v2 Broadcast Audio Extension chunk."""
    fixed = struct.pack(
        "<256s32s32s10s8sIIH64s5h180s",
        _ascii(description, 256),
        _ascii(originator, 32),
        _ascii(reference, 32),
        when.strftime("%Y-%m-%d").encode(),
        when.strftime("%H:%M:%S").encode(),
        0, 0,                         # TimeReference low/high (sample 0)
        2,                            # Version
        b"",                          # UMID (unused)
        0x7FFF, 0x7FFF, 0x7FFF, 0x7FFF, 0x7FFF,   # loudness fields: not measured
        b"",                          # reserved
    )
    return _chunk(b"bext", fixed + _ascii(coding_history))


def info_list_chunk(fields):
    """RIFF LIST/INFO chunk from {4-char id: text}."""
    body = b"INFO"
    for cid, text in fields.items():
        body += _chunk(cid.encode(), _ascii(text) + b"\x00")
    return _chunk(b"LIST", body)


def transcript_chunk(text):
    """STUB: narration text inside the file (adtl/ltxt, purpose 'tran').

    The archival doc points at the Labeled Text chunk, which needs a cue point
    to anchor to. Not implemented yet - returns None so nothing is written.
    """
    return None


def to_bwf(wav, meta):
    """Insert bext after fmt, LIST/INFO before data. Audio bytes untouched."""
    if wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    chunks, pos = [], 12
    while pos + 8 <= len(wav):
        cid = wav[pos:pos + 4]
        size = struct.unpack("<I", wav[pos + 4:pos + 8])[0]
        chunks.append((cid, wav[pos + 8:pos + 8 + size]))
        pos += 8 + size + (size & 1)

    bext = bext_chunk(**meta["bext"])
    info = info_list_chunk(meta["info"])
    tran = transcript_chunk(meta.get("text", ""))

    out = b""
    for cid, data in chunks:
        if cid == b"data":
            out += info + (tran or b"")
        out += _chunk(cid, data)
        if cid == b"fmt ":
            out += bext
    return b"RIFF" + struct.pack("<I", 4 + len(out)) + b"WAVE" + out


# ------------------------------------------------------------ synthesis -----
class ApiError(Exception):
    def __init__(self, http, code, message, **extra):
        super().__init__(message)
        self.http, self.code, self.message, self.extra = http, code, message, extra


def synthesize(text, voice):
    """Run Piper once, as a child process, and return WAV bytes."""
    with tempfile.TemporaryDirectory(prefix="pepa-tts-") as tmp:
        src = Path(tmp) / "input.txt"
        out = Path(tmp) / "out.wav"
        src.write_text(text, encoding="utf-8")
        cmd = PIPER_CMD + ["-m", voice, "--data-dir", str(VOICES_DIR),
                           "-i", str(src), "-f", str(out)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=SYNTH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ApiError(500, "synthesis_failed",
                           f"Piper did not finish within {SYNTH_TIMEOUT_S}s.")
        if r.returncode != 0 or not out.exists() or out.stat().st_size <= 44:
            tail = (r.stderr or "").strip().splitlines()[-3:]
            raise ApiError(500, "synthesis_failed", "Piper exited with an error.",
                           detail=tail)
        return out.read_bytes()


def wav_seconds(wav):
    try:
        import io
        import wave
        with wave.open(io.BytesIO(wav)) as w:
            return round(w.getnframes() / w.getframerate(), 2)
    except Exception:
        return None


# ------------------------------------------------------------- handler ------
class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVICE}/{SERVICE_VERSION}"

    # -- helpers --
    def _json(self, http, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode()
        self.send_response(http)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, err):
        payload = {"status": "error", "code": err.code, "message": err.message}
        payload.update(err.extra)
        self._json(err.http, payload)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{dt.datetime.now().isoformat(timespec='seconds')} "
                         f"{self.client_address[0]} {fmt % args}\n")

    # -- routes --
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/voices":
            self._json(200, {"piper": sorted(list_voices())})
        elif path == "/health":
            self._json(200, {"status": "ok", "service": SERVICE,
                             "version": SERVICE_VERSION,
                             "busy": JOB_LOCK.locked(),
                             "piper": piper_version()})
        else:
            self._error(ApiError(404, "not_found", f"No route {path}."))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/synthesize":
            return self._error(ApiError(404, "not_found", f"No route {path}."))
        if not JOB_LOCK.acquire(blocking=False):
            return self._error(ApiError(503, "busy",
                                        "A synthesis job is already running. Retry later."))
        try:
            self._synthesize()
        except ApiError as e:
            self._error(e)
        except Exception as e:                      # last-resort guard
            self._error(ApiError(500, "synthesis_failed", f"Unexpected: {e}"))
        finally:
            JOB_LOCK.release()

    def _read_form(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            raise ApiError(400, "bad_request", "Send multipart/form-data.")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise ApiError(400, "bad_request",
                           f"Body must be 1..{MAX_UPLOAD_BYTES} bytes.")
        body = self.rfile.read(length)
        msg = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
        if not msg.is_multipart():
            raise ApiError(400, "bad_request", "Malformed multipart body.")
        fields, upload = {}, None
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            data = part.get_payload(decode=True) or b""
            if name == "file":
                upload = (part.get_filename() or "", data)
            elif name:
                fields[name] = data.decode("utf-8", "replace").strip()
        return fields, upload

    def _synthesize(self):
        started = time.monotonic()
        fields, upload = self._read_form()

        # -- validate request --
        if not upload or not upload[0]:
            raise ApiError(400, "bad_request", "Missing 'file' part with a filename.")
        filename = Path(upload[0]).name
        if not filename.lower().endswith(".md"):
            raise ApiError(400, "bad_request", "The file must be a .md file.",
                           requested={"file": filename})
        missing = [k for k in ("model", "voice") if not fields.get(k)]
        if missing:
            raise ApiError(400, "bad_request",
                           f"Missing required field(s): {', '.join(missing)}.")
        model, voice = fields["model"], fields["voice"]
        fmt = (fields.get("format") or "wav").lower()
        if fmt not in ("wav", "bwf"):
            raise ApiError(400, "bad_request", "format must be 'wav' or 'bwf'.",
                           requested={"format": fmt})

        voices = list_voices()
        requested = {"model": model, "voice": voice}
        if model != "piper":
            raise ApiError(404, "voice_unavailable",
                           f"Model '{model}' is not available.",
                           requested=requested, available={"piper": sorted(voices)})
        if voice not in voices:
            raise ApiError(404, "voice_unavailable",
                           f"Voice '{voice}' is not installed.",
                           requested=requested, available={"piper": sorted(voices)})

        try:
            raw_text = upload[1].decode("utf-8")
        except UnicodeDecodeError:
            raise ApiError(400, "bad_request", "The .md file must be UTF-8 text.")
        text, lint_fired = lint(raw_text)
        if not text:
            raise ApiError(422, "empty_text", "Nothing left to speak after linting.",
                           requested={"file": filename})

        # -- synthesize (Piper runs and exits) --
        wav = synthesize(text, voice)

        # -- package --
        job_id = uuid7()
        stem = filename[:-3]
        out_name = f"{stem}-bwf.wav" if fmt == "bwf" else f"{stem}.wav"
        text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rate = int(voices[voice].get("audio", {}).get("sample_rate", 0)) or None
        if fmt == "bwf":
            now = dt.datetime.now().astimezone()
            history = (f"A=PCM,F={rate},W=16,M=mono,"
                       f"T=piper {piper_version()};voice={voice};"
                       f"lint={'+'.join(lint_fired) or 'none'}\r\n")
            wav = to_bwf(wav, {
                "bext": {
                    "description": f"Synthetic speech. Source: {filename}",
                    "originator": "Pepa TTS (Mac Studio, piper)",
                    "reference": job_id.replace("-", ""),   # 32 chars fits the field
                    "when": now,
                    "coding_history": history,
                },
                "info": {
                    "INAM": stem,
                    "ICRD": now.strftime("%Y-%m-%d"),
                    "ISFT": f"{SERVICE} {SERVICE_VERSION}; piper {piper_version()}",
                    "ICMT": f"Synthetic speech. voice={voice}; text_sha256={text_sha}",
                },
                "text": text,
            })

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.send_header("Content-Disposition", f'attachment; filename="{out_name}"')
        self.send_header("X-Pepa-Job-Id", job_id)
        self.send_header("X-Pepa-Voice", voice)
        self.send_header("X-Pepa-Lint", ",".join(lint_fired) or "none")
        self.send_header("X-Pepa-Text-Sha256", text_sha)
        secs = wav_seconds(wav)
        if secs is not None:
            self.send_header("X-Pepa-Duration-Seconds", str(secs))
        self.send_header("X-Pepa-Elapsed-Seconds",
                         f"{time.monotonic() - started:.2f}")
        self.end_headers()
        self.wfile.write(wav)


# ----------------------------------------------------------------- main -----
def main():
    global VOICES_DIR
    ap = argparse.ArgumentParser(description="Pepa TTS endpoint")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--voices", type=Path,
                    default=Path(os.environ.get("PEPA_TTS_VOICES",
                                                Path.home() / "pepa-tts" / "voices")),
                    help="folder holding Piper .onnx + .onnx.json voice files")
    a = ap.parse_args()
    VOICES_DIR = a.voices.expanduser().resolve()
    if not VOICES_DIR.is_dir():
        sys.exit(f"voices folder not found: {VOICES_DIR}")
    print(f"{SERVICE} {SERVICE_VERSION} on {a.host}:{a.port}  "
          f"voices={VOICES_DIR}  piper={piper_version()}  "
          f"available={sorted(list_voices())}", file=sys.stderr)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()