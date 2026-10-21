#!/usr/bin/env python3
"""pepa_tts_client.py - send .md narration files to the Pepa TTS endpoint.

Each file is sent on its own; the returned WAV is saved next to the source
under the name the server gives (name.wav or name-bwf.wav). Errors are printed
and the next file is tried.

Examples
  python3 pepa_tts_client.py --voices
  python3 pepa_tts_client.py --voice en_US-lessac-medium *_draft*.md
  python3 pepa_tts_client.py --voice en_US-lessac-medium --format bwf slide02.md

Standard library only.
"""
import argparse
import json
import secrets
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

DEFAULT_URL = "http://192.168.10.5:8100"     # Mac Studio


def multipart(fields, file_field, path):
    boundary = "pepa" + secrets.token_hex(12)
    out = []
    for k, v in fields.items():
        out += [f"--{boundary}", f'Content-Disposition: form-data; name="{k}"', "", v]
    head = "\r\n".join(out + [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{file_field}"; filename="{path.name}"',
        "Content-Type: text/markdown; charset=utf-8", "", ""]).encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def filename_from(headers, fallback):
    m = Message()
    m["Content-Disposition"] = headers.get("Content-Disposition", "")
    return m.get_filename() or fallback


def send(url, path, model, voice, fmt):
    body, ctype = multipart({"model": model, "voice": voice, "format": fmt}, "file", path)
    req = urllib.request.Request(f"{url}/synthesize", data=body, method="POST",
                                 headers={"Content-Type": ctype,
                                          "Authorization": "Bearer none"})
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            name = filename_from(r.headers, path.stem + ".wav")
            dest = path.with_name(Path(name).name)
            dest.write_bytes(r.read())
            secs = r.headers.get("X-Pepa-Duration-Seconds", "?")
            took = r.headers.get("X-Pepa-Elapsed-Seconds", "?")
            print(f"ok    {path.name} -> {dest.name}  {secs}s audio, {took}s render")
            return True
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read())
            print(f"error {path.name}: {err.get('code')} - {err.get('message')}")
            if "available" in err:
                print(f"      available: {err['available']}")
        except ValueError:
            print(f"error {path.name}: HTTP {e.code}")
    except urllib.error.URLError as e:
        print(f"error {path.name}: cannot reach {url} ({e.reason})")
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--model", default="piper")
    ap.add_argument("--voice")
    ap.add_argument("--format", choices=["wav", "bwf"], default="wav")
    ap.add_argument("--voices", action="store_true", help="list installed voices and exit")
    a = ap.parse_args()

    if a.voices:
        with urllib.request.urlopen(f"{a.url}/voices", timeout=10) as r:
            print(json.dumps(json.loads(r.read()), indent=2))
        return
    if not a.files or not a.voice:
        ap.error("give --voice and at least one .md file (or use --voices)")
    ok = sum(send(a.url, p, a.model, a.voice, a.format) for p in a.files)
    print(f"{ok}/{len(a.files)} succeeded")
    sys.exit(0 if ok == len(a.files) else 1)


if __name__ == "__main__":
    main()