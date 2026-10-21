# pepa-tts — text to WAV on the Mac Studio

A small endpoint in the Mac Studio's inference arm. Send it a `.md` narration
file; it returns a `.wav` (or a BWF `-bwf.wav`) with the same base name, or a
JSON error. Piper does the speech, started per request and exited afterwards,
so nothing stays loaded between jobs.

Version 0.1.0 — 2026-10-20.

## Contract

| | |
|---|---|
| Port | **8100** (the 8100 block is for non-LLM endpoints) |
| Key | none required; clients may send `Authorization: Bearer none` |
| `POST /synthesize` | multipart form: `file` (.md), `model` (`piper`), `voice` (e.g. `en_US-lessac-medium`), `format` (`wav` default, or `bwf`) |
| Success | `200 audio/wav`, file `name.wav` or `name-bwf.wav` (in `Content-Disposition`) |
| `GET /voices` | `{"piper": ["en_US-lessac-medium", ...]}` — single-speaker voices only |
| `GET /health` | status, version, Piper version, whether a job is running |

Response headers on success: `X-Pepa-Job-Id` (UUIDv7), `X-Pepa-Voice`,
`X-Pepa-Lint`, `X-Pepa-Text-Sha256`, `X-Pepa-Duration-Seconds`,
`X-Pepa-Elapsed-Seconds`.

### Errors (JSON body, no audio)

| code | HTTP | when |
|---|---|---|
| `bad_request` | 400 | missing file/fields, not `.md`, not UTF-8, bad `format` |
| `voice_unavailable` | 404 | model isn't `piper`, or voice not installed (response lists what is) |
| `empty_text` | 422 | nothing left to speak after linting |
| `synthesis_failed` | 500 | Piper exited with an error or timed out |
| `busy` | 503 | a job is already running — one at a time |

### Processing

1. **Lint** — strips `**bold**`. Quote marks are left alone. Add rules to
   `LINT_RULES` in the server.
2. **Synthesize** — Piper, the voice's native sample rate (no resampling yet).
3. **Package** — plain WAV by default. With `format=bwf`:
   - `bext`: Description (`Synthetic speech. Source: <file>.md`), Originator
     (`Pepa TTS (Mac Studio, piper)`), OriginatorReference (job ID), date and
     time, CodingHistory (PCM, rate, 16-bit, mono, Piper version, voice, lint rules)
   - `LIST/INFO`: title, date, software, comment with voice and SHA-256 of the
     cleaned text
   - transcript (`tran`) chunk: **stub**, not written yet
   - the audio bytes are identical to the plain WAV

## Setup on the Mac Studio

```bash
mkdir -p ~/pepa-tts/voices && cd ~/pepa-tts
# copy pepa_tts_server.py here
uv venv .venv && uv pip install --python .venv piper-tts
.venv/bin/python -m piper.download_voices --data-dir voices en_US-lessac-medium
lsof -iTCP:8100 -sTCP:LISTEN        # should print nothing
.venv/bin/python pepa_tts_server.py --voices voices --port 8100
```

Started by hand in a terminal for now, like rapid-mlx. A LaunchDaemon can come later.

## Using it (from any machine)

```bash
python3 pepa_tts_client.py --voices
python3 pepa_tts_client.py --voice en_US-lessac-medium *_draft*.md
python3 pepa_tts_client.py --voice en_US-lessac-medium --format bwf Pepa_v2_slide02_narration_draft1.md
```

The client defaults to `http://192.168.10.5:8100`; override with `--url`.
WAVs land next to each `.md`. Or with curl:

```bash
curl -OJ -F file=@slide02.md -F model=piper -F voice=en_US-lessac-medium \
     http://192.168.10.5:8100/synthesize
```

## Not done yet

- Transcript inside the BWF (`tran` chunk) — stubbed.
- Resampling (e.g. to 48 kHz) — output is the voice's native rate.
- No pause between paragraphs; Piper's `--sentence-silence` is not exposed.
- mlxsw awareness of the 8100 endpoint.
- LaunchDaemon.

## Tested (2026-10-20, Linux sandbox)

Against a stand-in for Piper (real voices couldn't be downloaded there):
`/voices` and `/health`, wav and bwf success, every error code, the busy lock,
multi-speaker voices filtered out. The BWF was checked with ffprobe and Python's
`wave` module; audio bytes match the plain WAV. **Real Piper speech has not
been run** — first run on the Studio is the real test.
