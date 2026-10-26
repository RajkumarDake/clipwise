#!/usr/bin/env bash
# clipwise one-time setup. Read it before you run it; it only touches $CLIPWISE_HOME.
#
#   bash setup.sh                 # venv + python deps + face model + caption font + whisper model
#   bash setup.sh --no-model      # skip pre-downloading the whisper model (it downloads on first use)
#   bash setup.sh --no-pip        # reuse an env you manage yourself (with CLIPWISE_VENV=/path/to/venv)
#
# Environment:
#   CLIPWISE_HOME   where everything goes (default ~/.clipwise)
#   CLIPWISE_VENV   python venv to create or reuse (default $CLIPWISE_HOME/venv)
#   CLIPWISE_MODEL  whisper model to pre-download (default large-v3-turbo)
#   PYTHON          python interpreter used to create the venv (default python3)
#
# Safe to re-run: every step is skipped when it is already done.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIPWISE_HOME="${CLIPWISE_HOME:-$HOME/.clipwise}"
VENV="${CLIPWISE_VENV:-$CLIPWISE_HOME/venv}"
MODEL="${CLIPWISE_MODEL:-large-v3-turbo}"
PY="${PYTHON:-python3}"
DO_PIP=1
DO_MODEL=1
for a in "$@"; do
  case "$a" in
    --no-pip) DO_PIP=0 ;;
    --no-model) DO_MODEL=0 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

# Pinned downloads, verified by sha256.
YUNET_URL="https://github.com/opencv/opencv_zoo/raw/f12e12798e8314f7c074a6656816c048dcc95b7a/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_SHA="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
FONT_URL="https://raw.githubusercontent.com/google/fonts/f3b885d5590e307e02542f1a724cec55d567fdaa/ofl/lato/Lato-Black.ttf"
FONT_SHA="808c62839c62dbce7de689af7603666fc7f8b81e0df537d8a5212c87580d4337"

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

download() {  # url dest
  if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 3 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then wget -q -O "$2" "$1"
  else "$PY" -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"; fi
}

fetch_verified() {  # url dest sha256
  local url="$1" dest="$2" want="$3"
  if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$want" ]; then ok "$(basename "$dest") already present"; return 0; fi
  mkdir -p "$(dirname "$dest")"
  download "$url" "$dest.part" || { rm -f "$dest.part"; return 1; }
  local got; got="$(sha256_of "$dest.part")"
  if [ "$got" != "$want" ]; then
    rm -f "$dest.part"
    fail "checksum mismatch for $(basename "$dest"): got $got, expected $want"
  fi
  mv "$dest.part" "$dest"
  ok "$(basename "$dest") downloaded, sha256 verified"
}

say "Checking python (needs 3.10+)"
command -v "$PY" >/dev/null 2>&1 || fail "python3 not found. Install Python 3.10+:
    Debian/Ubuntu: sudo apt install python3 python3-venv
    macOS:         brew install python
    Windows:       use WSL2 (Ubuntu) and follow the Debian/Ubuntu line"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "$("$PY" -V 2>&1) is too old; clipwise needs Python 3.10+ (set PYTHON=/path/to/python3.x)"
ok "$("$PY" -V 2>&1)"

say "Checking ffmpeg (needs libass for captions)"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  fail "ffmpeg/ffprobe not found. Install it:
    Debian/Ubuntu: sudo apt install ffmpeg
    Fedora:        sudo dnf install ffmpeg   (RPM Fusion)
    macOS:         brew install ffmpeg
    Windows:       use WSL2 (Ubuntu) and 'sudo apt install ffmpeg'"
fi
filters="$(ffmpeg -hide_banner -filters 2>/dev/null)"
grep -qE '^ *[.A-Z|]+ +ass ' <<<"$filters" \
  || fail "your ffmpeg has no libass ('ass' filter), so captions cannot be burned in. Install a full build:
    Debian/Ubuntu: sudo apt install ffmpeg      macOS: brew install ffmpeg"
for f in loudnorm sidechaincompress silencedetect; do
  grep -qE " $f " <<<"$filters" || fail "your ffmpeg lacks the '$f' filter; install a full ffmpeg build"
done
ok "$(ffmpeg -hide_banner -version 2>/dev/null | sed -n 1p)"

say "Python environment: $VENV"
if [ -x "$VENV/bin/python" ]; then
  ok "reusing existing venv"
else
  "$PY" -m venv "$VENV" || fail "could not create a venv (Debian/Ubuntu: sudo apt install python3-venv)"
  ok "created"
fi
VPY="$VENV/bin/python"
if [ "$DO_PIP" = 1 ]; then
  "$VPY" -m pip install --upgrade --quiet pip
  "$VPY" -m pip install --quiet -r "$HERE/requirements.txt"
  ok "installed requirements.txt"
else
  ok "--no-pip: not installing packages"
fi
"$VPY" - <<'PYCHECK' || fail "python packages are missing or broken in $VENV (re-run without --no-pip)"
import cv2, numpy, faster_whisper
assert hasattr(cv2, "FaceDetectorYN"), "opencv without FaceDetectorYN (need opencv >= 4.8)"
print("    ok: numpy", numpy.__version__, "| opencv", cv2.__version__, "| faster-whisper", faster_whisper.__version__)
PYCHECK

say "Face detector (OpenCV YuNet, MIT)"
fetch_verified "$YUNET_URL" "$CLIPWISE_HOME/models/yunet.onnx" "$YUNET_SHA"

say "Caption font (Lato Black, SIL OFL)"
fetch_verified "$FONT_URL" "$CLIPWISE_HOME/fonts/Lato-Black.ttf" "$FONT_SHA" \
  || printf '    warn: font download failed; captions will fall back to DejaVu Sans\n'

if [ "$DO_MODEL" = 1 ]; then
  say "Whisper model '$MODEL' (one-time download, about 1.6 GB for large-v3-turbo)"
  "$VPY" - "$MODEL" <<'PYMODEL'
import sys
from faster_whisper import WhisperModel
name = {"large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo"}.get(sys.argv[1], sys.argv[1])
WhisperModel(name, device="cpu", compute_type="int8")
print("    ok: model cached")
PYMODEL
else
  ok "--no-model: the whisper model downloads on the first transcribe"
fi

say "Done"
cat <<EOF
clipwise is ready. Runtime home: $CLIPWISE_HOME
$( [ "$VENV" != "$CLIPWISE_HOME/venv" ] && echo "Custom venv: export CLIPWISE_VENV=$VENV before using clipwise." )
$( [ "$CLIPWISE_HOME" != "$HOME/.clipwise" ] && echo "Custom home: export CLIPWISE_HOME=$CLIPWISE_HOME before using clipwise." )
Check:  python3 "$HERE/scripts/run.py" check
Then ask your agent something like:  "Make 3 Shorts from <video link or file>"
EOF
