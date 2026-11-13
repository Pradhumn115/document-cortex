#!/usr/bin/env bash
# Resumable Hugging Face repo download (curl -C - per file, retries on stalls).
# Usage: scripts/hf_fetch.sh <org/repo> [dest_root] [exclude_regex]
# Saves to <dest_root>/<org>/<repo>/ (default dest_root: ~/.lmstudio/models), where
# LM Studio lists it and mlx_lm can load it by path. Files whose path matches
# exclude_regex (e.g. '^onnx/') are skipped.
set -u
REPO="$1"
ROOT="${2:-$HOME/.lmstudio/models}"
EXCLUDE="${3:-}"
DEST="$ROOT/$REPO"
mkdir -p "$DEST" || exit 1
cd "$DEST" || exit 1

FILES=$(python3 -I -c "
import sys
from huggingface_hub import HfApi
i = HfApi().model_info(sys.argv[1], files_metadata=True)
for s in i.siblings:
    print(f'{s.rfilename}\t{s.size or 0}')
" "$REPO") || { echo "could not list $REPO"; exit 1; }

while IFS=$'\t' read -r f size; do
  [ -z "$f" ] && continue
  if [ -n "$EXCLUDE" ] && [[ "$f" =~ $EXCLUDE ]]; then continue; fi
  mkdir -p "$(dirname "$f")"
  for i in $(seq 1 200); do
    have=$(stat -f %z "$f" 2>/dev/null || echo 0)
    [ "$have" -ge "$size" ] && break
    curl -sS -L -C - --speed-limit 50000 --speed-time 60 --connect-timeout 30 \
      -o "$f" "https://huggingface.co/$REPO/resolve/main/$f"
  done
  have=$(stat -f %z "$f" 2>/dev/null || echo 0)
  [ "$have" -ge "$size" ] || { echo "FAILED $REPO/$f at $have of $size bytes"; exit 1; }
done <<< "$FILES"
echo "complete: $DEST"
