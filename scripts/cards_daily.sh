#!/usr/bin/env bash
# Write index cards for the whole book on a free API tier with a daily request cap.
# Runs `cortex cards --by-section`; when the provider's daily quota is used up
# (exit 75) it waits and resumes. Cards already written are cached, so nothing
# is paid for twice. Stop it any time with Ctrl-C / kill; rerun to continue.
#
# Usage: [GROQ_ENV=path/to/.env] scripts/cards_daily.sh [model]   (default keys: ~/.cortex/.env)
set -u
cd "$(dirname "$0")/.." || exit 1
MODEL="${1:-openai/gpt-oss-120b}"
ENV_FILE="${GROQ_ENV:-$HOME/.cortex/.env}"
export OPENAI_API_KEY
OPENAI_API_KEY=$(grep -E '^\s*GROQ_API_KEY\s*=' "$ENV_FILE" | head -1 | sed -E 's/^[^=]*=\s*//; s/["'"'"' ]//g')
[ -n "$OPENAI_API_KEY" ] || { echo "no GROQ_API_KEY in $ENV_FILE"; exit 1; }
LOG=data/bhav/cards_run.log

while true; do
  echo "== $(date '+%F %T') starting run ($MODEL)" | tee -a "$LOG"
  python3 -m cortex cards data/bhav --provider openai --base-url https://api.groq.com/openai/v1 \
    --model "$MODEL" --workers 2 --by-section >> "$LOG" 2>&1
  code=$?
  if [ $code -eq 0 ]; then
    echo "== $(date '+%F %T') all cards written" | tee -a "$LOG"; exit 0
  elif [ $code -eq 75 ]; then
    echo "== $(date '+%F %T') daily quota used up; sleeping 30 min" | tee -a "$LOG"; sleep 1800
  else
    echo "== $(date '+%F %T') run failed (exit $code); retrying in 5 min" | tee -a "$LOG"; sleep 300
  fi
done
