#!/bin/sh
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJ_DIR=$(dirname "$DIR")
cd "$PROJ_DIR"

# Override per environment, e.g.:
#   PORT=8086 OIC_ENV_FILE=.env.prod ./scripts/run-local.sh
# HOST defaults to loopback only. Set HOST=0.0.0.0 to expose it on the
# network (put TLS and access control in front of it if you do).
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8085}

if [ ! -d .venv ]; then
	python3 -m venv .venv
fi
. .venv/bin/activate
pip -q install -r requirements.txt

exec uvicorn mcp_server.main:app --host "$HOST" --port "$PORT" --ws websockets
