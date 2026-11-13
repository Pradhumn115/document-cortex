#!/usr/bin/env bash
# Benchmark one local MLX model: start mlx_lm.server, run `cortex bench`, stop it.
# Usage: scripts/bench_mlx.sh <org/repo> <name> [--reasoning nothink|low]
set -u
REPO="$1"; NAME="$2"; shift 2
MODEL_DIR="$HOME/.lmstudio/models/$REPO"
PORT=8089
cd "$(dirname "$0")/.." || exit 1
[ -f "$MODEL_DIR/config.json" ] || { echo "model not downloaded: $MODEL_DIR"; exit 1; }

mlx_lm.server --model "$MODEL_DIR" --port "$PORT" --max-tokens 3000 > "data/bench/logs/server-$NAME.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT

for i in $(seq 1 120); do
  curl -s -m 2 "http://127.0.0.1:$PORT/v1/models" >/dev/null && break
  kill -0 $SERVER 2>/dev/null || { echo "server died"; tail -20 "data/bench/logs/server-$NAME.log"; exit 1; }
  sleep 2
done

OPENAI_API_KEY=local python3 -m cortex bench data/bhav --base-url "http://127.0.0.1:$PORT/v1" \
  --model "$MODEL_DIR" --name "$NAME" "$@" 2>&1 | tail -8
