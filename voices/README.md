# voices/

This folder holds the Piper voice files the server can use. It is empty in
the repo on purpose.

## Why it's empty

- **Size.** Each voice is an `.onnx` model of roughly 60 MB plus a small
  `.onnx.json` config. They don't belong in git.
- **Choice.** Which voices a machine offers is a local decision. The server
  lists whatever is in this folder; nothing is hard-coded.
- **Licenses.** Each voice carries its own license and source (see its
  model card). Keeping them out of the repo keeps the repo's MIT license clean.

## Adding a voice

From the project folder:

    .venv/bin/python -m piper.download_voices --data-dir voices en_US-lessac-medium

That writes `en_US-lessac-medium.onnx` and `en_US-lessac-medium.onnx.json`
here. Restart is not needed; the server checks this folder on every request.

## Rules

- Both files must be present, or the voice is ignored.
- Single-speaker voices only. Multi-speaker models are skipped.
- The voice name is the file name without `.onnx`, and is what a client
  sends as `voice`.

Check what the server sees:

    python3 pepa_tts_client.py --url http://127.0.0.1:8100 --voices