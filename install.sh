#!/usr/bin/env bash
# HaramMute local server for Linux: one-shot installer.
#
#   ./install.sh            # auto-detect GPU, install, enable the user service
#   ./install.sh --accel cpu|cuda|openvino
#   ./install.sh --no-service
#
# Needs: bash, curl, ffmpeg (with ffprobe). deno is recommended for yt-dlp.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ACCEL="auto"
SERVICE=1
PY_VERSION="3.12"

while [ $# -gt 0 ]; do
  case "$1" in
    --accel) ACCEL="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- system deps
[ "$(uname -s)" = "Linux" ] || die "this installer is for Linux"
for tool in ffmpeg ffprobe curl; do
  command -v "$tool" >/dev/null || die "$tool is required. Install it with your package manager (e.g. 'sudo pacman -S ffmpeg', 'sudo apt install ffmpeg')."
done
command -v deno >/dev/null || warn "deno not found. yt-dlp needs it for some YouTube videos: https://deno.com (or 'pacman -S deno', 'apt install deno')."

# ---------------------------------------------------------------- uv
if ! command -v uv >/dev/null; then
  say "installing uv (Python package manager) into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || die "uv install failed; install it manually from https://docs.astral.sh/uv/"
fi

# ---------------------------------------------------------------- accelerator
detect_accel() {
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then echo cuda; return; fi
  if [ -d /dev/dri ] && grep -qi 'intel' /sys/class/drm/card*/device/vendor 2>/dev/null; then :; fi
  if [ -d /dev/dri ] && lspci 2>/dev/null | grep -iE 'vga|3d|display' | grep -qi intel; then echo openvino; return; fi
  echo cpu
}
[ "$ACCEL" = "auto" ] && ACCEL="$(detect_accel)"
case "$ACCEL" in
  cuda)     ORT_PKG="onnxruntime-gpu";      TORCH_INDEX="https://download.pytorch.org/whl/cu126" ;;
  openvino) ORT_PKG="onnxruntime-openvino"; TORCH_INDEX="https://download.pytorch.org/whl/cpu" ;;
  cpu)      ORT_PKG="onnxruntime";          TORCH_INDEX="https://download.pytorch.org/whl/cpu" ;;
  *) die "unknown --accel '$ACCEL' (cpu, cuda, openvino)" ;;
esac
say "accelerator: $ACCEL ($ORT_PKG)"

# ---------------------------------------------------------------- python env
cd "$HERE"
say "creating Python $PY_VERSION environment in .venv"
uv venv --python "$PY_VERSION" .venv -q
PY=".venv/bin/python"
say "installing torch (from $TORCH_INDEX)"
uv pip install -q --python "$PY" torch torchvision --index-url "$TORCH_INDEX"
say "installing audio-separator and the server"
uv pip install -q --python "$PY" -r requirements.txt
# audio-separator[cpu] pins plain onnxruntime; swap in the accelerated build when asked.
if [ "$ORT_PKG" != "onnxruntime" ]; then
  uv pip uninstall -q --python "$PY" onnxruntime 2>/dev/null || true
  uv pip install -q --python "$PY" "$ORT_PKG"
fi
"$PY" - <<'PYEOF'
import onnxruntime, torch, torchvision, librosa, audioread, audio_separator, yt_dlp, fastapi
print("python deps ok:", "onnxruntime", onnxruntime.__version__, onnxruntime.get_available_providers())
PYEOF

# ---------------------------------------------------------------- model
MODEL_DIR="$HERE/model_cache/audio-separator"
MODEL="$MODEL_DIR/UVR-MDX-NET-Voc_FT.onnx"
if [ ! -f "$MODEL" ]; then
  say "downloading the UVR-MDX-NET-Voc_FT vocal model (67 MB)"
  mkdir -p "$MODEL_DIR"
  curl -L --fail --progress-bar -o "$MODEL" \
    "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-MDX-NET-Voc_FT.onnx"
fi

# ---------------------------------------------------------------- service
chmod +x "$HERE/run.sh"
if [ "$SERVICE" = 1 ]; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/harammute.service" <<EOF
[Unit]
Description=HaramMute local server (Linux port)
After=network.target

[Service]
Type=simple
ExecStart=$HERE/run.sh
WorkingDirectory=$HERE
Restart=on-failure
RestartSec=5
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Nice=10
CPUWeight=50

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now harammute.service
  say "waiting for the server"
  for _ in $(seq 1 60); do
    curl -sf http://127.0.0.1:8765/health >/dev/null && break
    sleep 1
  done
  curl -sf http://127.0.0.1:8765/client-info >/dev/null || die "server did not come up; see: journalctl --user -u harammute -n 50"
  say "server running: http://127.0.0.1:8765 (starts at login; 'systemctl --user status harammute')"
else
  say "run it with: $HERE/run.sh"
fi
say "done. In the HaramMute extension popup choose 'On Your Computer'."
