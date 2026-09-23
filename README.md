# Linux server for the HaramMute extension

Local server that lets the [HaramMute](https://haram-mute.com/) browser extension
work on Linux in its free **On Your Computer** mode. The extension removes
background music from YouTube videos; on Windows it does that by talking to a
desktop app on `127.0.0.1:8765`. There is no Linux desktop app. This is one.

**Third-party, independent project.** Made by a user, not by HaramMute. Not
affiliated with, endorsed by, or supported by HaramMute. They do not maintain
or plan a Linux build, so please report issues here, not to them. Clean reimplementation of the local API from observing the
extension's requests; the extension itself is unchanged and none of its code
is included.

## Install

```bash
git clone https://github.com/zatreby/linux-for-harammute
cd linux-for-harammute
./install.sh
```

The installer creates a Python environment, picks CUDA (NVIDIA), OpenVINO
(Intel) or plain CPU, downloads the 67 MB vocal model, and installs a user
systemd service that starts at login. Then open the extension popup and choose
**On Your Computer**. It should say "Ready, Connected to server".

Requirements: `ffmpeg` (with `ffprobe`) and `curl`. `deno` is recommended
(yt-dlp uses it for some YouTube videos). Roughly 3.5 GB of RAM free while a
video is processing.

Options: `./install.sh --accel cpu|cuda|openvino`, `./install.sh --no-service`.

## Use

Click the HaramMute button in the YouTube player. The first 30 s of vocals-only
audio arrives after about 30 s, then playback follows as parts are processed.

```bash
systemctl --user status harammute      # restart / stop
journalctl --user -u harammute -f      # logs
```

Settings are environment variables in the service unit, all with the
`HARAMMUTE_` prefix; see `server/app/config.py`. The useful ones:

- `HARAMMUTE_COOKIES_FILE=/path/cookies.txt` or `HARAMMUTE_BROWSER_FOR_COOKIES=brave`
  when YouTube says "sign in to confirm you're not a bot". By default the server
  first tries without cookies, then with cookies from an installed browser.
- `HARAMMUTE_ORT_PROVIDERS=CPUExecutionProvider` to force a backend.
- `HARAMMUTE_MIN_FREE_MB=2000` refuse a job below this much free RAM.

Data (jobs, cached results for 24 h) lives in `~/.local/share/HaramMute`.

## How it works

yt-dlp downloads the audio, ffmpeg splits it into 30 s chunks, and
[audio-separator](https://github.com/nomadkaraoke/python-audio-separator) runs
the UVR-MDX-NET-Voc_FT model to keep only vocals. Each chunk is served as MP3
as soon as it is done, which is what lets the extension start early.

Separation runs in a child process per job, so the ~3 GB it uses is returned
when the job ends. If the extension stops polling a job for 90 s (closed tab,
cancelled), the job is cancelled.

Measured on a TigerLake i5 laptop, 8 GB, no discrete GPU: OpenVINO backend
about 0.9x realtime (a 10 min video in ~9 min), plain CPU about 1.5x.

## API

What the extension calls, all on `127.0.0.1:8765` (falls back to 8766-8768):

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/client-info` | identity: `status: ok`, `client: harammute-desktop` |
| GET | `/health` | `status: ok`, semver `version` |
| POST | `/jobs` | `{url, stems: 2, skip_cache?}` -> 202 with `job_id` |
| GET | `/jobs/{id}` | status, `progress`, `chunks[]` with `start_time` / `file_ready` |
| GET | `/jobs/{id}/chunks/{i}/vocals` | MP3 for one chunk, 202 while not ready |
| GET | `/jobs/{id}/stems/vocals` | full vocals MP3 once completed |

## License and third-party notices

This project is MIT licensed (see `LICENSE`). It is an independent project
and is not affiliated with, endorsed by, or maintained by HaramMute. The
extension itself is HaramMute's; this repository contains none of its code.

What it builds on, all installed or downloaded at install time:

| Component | License | Role |
| --- | --- | --- |
| [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) | MIT | runs the separation model |
| `UVR-MDX-NET-Voc_FT.onnx` from [Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui) | MIT | vocal separation model (downloaded, not bundled) |
| `model_cache/audio-separator/*.json` | MIT (UVR / audio-separator model lists) | pinned model metadata so installs are reproducible |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense | audio download |
| [FastAPI](https://github.com/fastapi/fastapi) / [uvicorn](https://github.com/encode/uvicorn) | MIT / BSD-3 | HTTP server |
| [librosa](https://github.com/librosa/librosa), [pydub](https://github.com/jiaaro/pydub), [audioread](https://github.com/beetbox/audioread) | ISC / MIT / MIT | audio I/O |
| [onnxruntime](https://github.com/microsoft/onnxruntime) (+ OpenVINO or CUDA build) | MIT | model inference |
| ffmpeg | LGPL/GPL, system package | chunking and MP3 encoding, not redistributed |
