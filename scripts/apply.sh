#!/usr/bin/env bash
# Re-run this any time you change API keys in .env or re-run the model prober.
set -e
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
HOSTPWD="$(pwd -W 2>/dev/null || pwd)"
echo "stopping open-webui..."
docker stop open-webui >/dev/null 2>&1 || true
MSYS_NO_PATHCONV=1 docker run --rm -i \
  -v open-webui:/data \
  -v "$HOSTPWD/functions:/src:ro" \
  -v "$HOSTPWD:/out:ro" \
  -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}" \
  -e GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
  python:3.11-slim python - < scripts/configure_providers.py
echo "starting open-webui..."
docker start open-webui >/dev/null
echo "done. give it ~30s to come up."
