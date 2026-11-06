#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${P0_VENV:-/tmp/k8s-lab-p0-venv}"
PY="$VENV/bin/python"
CONTAINER="k8s-lab-p0-pg-$$"
DATABASE="control_plane_test_p0_$(date +%s)_$$"

if ! command -v podman >/dev/null 2>&1; then
  echo "podman is required in WSL for P0 tests" >&2
  exit 2
fi

if [ ! -x "$PY" ]; then
  python3 -m venv "$VENV"
fi

if ! "$PY" -c "import sqlalchemy, pg8000" >/dev/null 2>&1; then
  "$VENV/bin/pip" install --disable-pip-version-check -q \
    "sqlalchemy==2.0.49" "pg8000==1.31.5"
fi

cleanup() {
  podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

podman run -d --rm --name "$CONTAINER" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e POSTGRES_DB="$DATABASE" \
  -p 127.0.0.1::5432 \
  docker.io/library/postgres:17-alpine >/dev/null

ready=0
for _ in $(seq 1 120); do
  if podman exec "$CONTAINER" pg_isready -U postgres -d "$DATABASE" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done

if [ "$ready" -ne 1 ]; then
  echo "PostgreSQL test container did not become ready" >&2
  exit 2
fi

PORT="$(podman port "$CONTAINER" 5432/tcp | head -n1 | sed 's/.*://')"
export CONTROL_PLANE_TEST_DATABASE_URL="postgresql+pg8000://postgres@127.0.0.1:${PORT}/${DATABASE}"
export CONTROL_PLANE_REQUIRE_TEST_DATABASE=1

cd "$ROOT"
"$PY" -B -m unittest discover -s tests_control_plane -t . -v "$@"
