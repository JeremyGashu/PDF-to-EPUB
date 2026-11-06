#!/usr/bin/env bash
# One command: build (first run) and start the PDF -> EPUB app, then open it.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop and try again." >&2
  exit 1
fi

echo "Building and starting PDF → EPUB (first build takes a few minutes)..."
PORT="$PORT" docker compose up -d --build

printf "Waiting for the app"
for _ in $(seq 1 60); do
  if curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
    echo " ready."
    echo "→ ${URL}"
    case "$(uname)" in
      Darwin) open "$URL" ;;
      Linux) command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 || true ;;
    esac
    echo "Stop it with: docker compose down"
    exit 0
  fi
  printf "."
  sleep 1
done
echo
echo "The app did not become healthy. Logs:" >&2
docker compose logs --tail 50 >&2
exit 1
