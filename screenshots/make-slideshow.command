#!/bin/bash
# Build slideshow.gif from all still images in this folder.
# Double-click on macOS (runs in Terminal) or run from a shell:
#   ./make-slideshow.command
# Each image shows 2 seconds, then crossfades into the next one over
# 0.5 seconds (the last one fades back into the first, so the loop is
# seamless). Output width is 720 px with 256 colors and no dithering: the
# flat UI needs the full palette (128 colors collapsed the subtle zebra
# stripes) and dithering both hid them and bloated the file; the lower width
# keeps the GIF smaller than the old 960/128 build. The Pillow fallback
# quantizes against ONE global palette built from a montage of all frames, so
# the tones stay stable across frames (no flicker). The GIF loops forever.
# Pipeline: Python 3 + Pillow always renders the frame sequence (holds +
# blended transition frames); ffmpeg is used when available to assemble
# the frames with a high-quality palette, otherwise Pillow assembles.
# The output (slideshow.gif) is referenced by the repo README and is
# excluded from the Kodi addon sync and the zip build (repo only).
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$DIR/slideshow.gif"
WIDTH=720
COLORS=256
HOLD=2
FADE=0.5
FPS=10

LIST="$(mktemp /tmp/slideshow.XXXXXX)"
FRAMES="$(mktemp -d /tmp/slideshow-frames.XXXXXX)"
trap 'rm -f "$LIST"; rm -rf "$FRAMES"' EXIT
find "$DIR" -maxdepth 1 -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' \) \
  ! -name 'slideshow.gif' | sort > "$LIST"
COUNT="$(grep -c . "$LIST" || true)"
if [ "$COUNT" -eq 0 ]; then
  echo "ERROR: no screenshots found in $DIR (png/jpg/jpeg)." >&2
  exit 1
fi
if ! python3 -c "import PIL.Image" >/dev/null 2>&1; then
  echo "ERROR: need Python 3 + Pillow (pip install pillow)." >&2
  exit 1
fi
echo "Found $COUNT image(s). Rendering holds plus crossfades..."

python3 - "$LIST" "$FRAMES" "$WIDTH" "$HOLD" "$FADE" "$FPS" <<'EOF'
import sys
from PIL import Image
items = [l.rstrip("\n") for l in open(sys.argv[1]) if l.strip()]
framedir, width = sys.argv[2], int(sys.argv[3])
hold_n = int(round(float(sys.argv[4]) * int(sys.argv[6])))
fade_n = int(round(float(sys.argv[5]) * int(sys.argv[6])))
shots = []
for p in items:
    im = Image.open(p).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    shots.append(im)
w = max(im.width for im in shots)
h = max(im.height for im in shots)
placed = []
for im in shots:
    bg = Image.new("RGB", (w, h), (0, 0, 0))
    bg.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    placed.append(bg)
idx = 0
for i, im in enumerate(placed):
    for _ in range(hold_n):
        im.save("%s/%04d.png" % (framedir, idx))
        idx += 1
    if len(placed) > 1:
        nxt = placed[(i + 1) % len(placed)]
        for step in range(1, fade_n + 1):
            Image.blend(im, nxt, step / fade_n).save("%s/%04d.png" % (framedir, idx))
            idx += 1
print("Rendered %d frames." % idx)
EOF

if command -v ffmpeg >/dev/null 2>&1; then
  echo "Assembling with ffmpeg."
  ffmpeg -y -v error -framerate "$FPS" -i "$FRAMES/%04d.png" -vf \
    "split[s0][s1];[s0]palettegen=max_colors=${COLORS}[p];[s1][p]paletteuse=dither=bayer" \
    "$OUT"
else
  echo "Assembling with Pillow (ffmpeg not found; install it for better quality)."
  python3 - "$FRAMES" "$OUT" "$FPS" "$COLORS" <<'EOF'
import glob
import sys
from PIL import Image
paths = sorted(glob.glob(sys.argv[1] + "/*.png"))
imgs = [Image.open(p).convert("RGB") for p in paths]
# ONE global palette for every frame: per-frame quantization (and 128 colors)
# shifted the subtle zebra tones between frames and collapsed them -- a shared
# 256-color palette built from a montage of all frames keeps the flat UI tones
# stable and the stripes visible.
w, h = imgs[0].size
sw, sh = max(1, w // 6), max(1, h // 6)
cols = 12
rows = (len(imgs) + cols - 1) // cols
montage = Image.new("RGB", (sw * cols, sh * rows))
for i, im in enumerate(imgs):
    montage.paste(im.resize((sw, sh), Image.LANCZOS), ((i % cols) * sw, (i // cols) * sh))
pal = montage.quantize(colors=int(sys.argv[4]), method=Image.MEDIANCUT)
frames = [im.quantize(palette=pal, dither=Image.NONE) for im in imgs]
frames[0].save(sys.argv[2], save_all=True, append_images=frames[1:],
               duration=int(round(1000 / int(sys.argv[3]))), loop=0, optimize=True)
EOF
fi

echo "Wrote $OUT ($(du -h "$OUT" | cut -f1))."
