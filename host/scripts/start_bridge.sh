#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$BRIDGE_DIR/.env"
VENV_PYTHON="$BRIDGE_DIR/.venv/bin/python"
if [[ ! -x "$VENV_PYTHON" && -x "$BRIDGE_DIR/.venv/Scripts/python.exe" ]]; then
  VENV_PYTHON="$BRIDGE_DIR/.venv/Scripts/python.exe"
fi

echo ""
echo "============================================================"
echo "  Glint Studio bridge + tunnel launcher"
echo "============================================================"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[ERROR] Python virtualenv not found at '$VENV_PYTHON'." >&2
  echo "Run inside '$BRIDGE_DIR':" >&2
  echo "  python3 -m venv .venv && ./.venv/bin/python -m pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ERROR] Missing '$ENV_FILE'. Copy '.env.example' to '.env' and set API_KEY first." >&2
  exit 1
fi

read_env_var() {
  local key="$1"
  local default_val="$2"
  local line val
  line="$(grep -E "^[[:space:]]*${key}=" "$ENV_FILE" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    echo "$default_val"
    return
  fi
  val="${line#*=}"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  val="${val%\"}"
  val="${val#\"}"
  val="${val%\'}"
  val="${val#\'}"
  echo "${val:-$default_val}"
}

find_bin() {
  local name="$1"
  shift
  if command -v "$name" >/dev/null 2>&1; then
    command -v "$name"
    return 0
  fi
  for candidate in "$@"; do
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

port_is_listening() {
  local host="$1"
  local port="$2"
  "$VENV_PYTHON" - "$host" "$port" <<'PY'
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(0.3)
try:
    sys.exit(0 if s.connect_ex((host, port)) == 0 else 1)
finally:
    s.close()
PY
}

get_tailscale_url() {
  local ts_bin="$1"
  local json_out
  json_out="$("$ts_bin" status --json 2>/dev/null || true)"
  if [[ -z "$json_out" ]]; then
    return 1
  fi
  "$VENV_PYTHON" - "$json_out" <<'PY'
import json, sys
try:
    data = json.loads(sys.argv[1])
    dns = (data.get("Self") or {}).get("DNSName") or ""
    dns = dns.rstrip(".")
    if dns:
        print(f"https://{dns}")
except Exception:
    pass
PY
}

show_tunnel_ready() {
  local local_url="$1"
  local public_url="$2"
  local note="$3"
  local clip_msg=""
  if [[ -n "$public_url" ]]; then
    if command -v pbcopy >/dev/null 2>&1; then
      printf '%s' "$public_url" | pbcopy || true
      clip_msg="(Public URL copied to clipboard via pbcopy!)"
    elif command -v xclip >/dev/null 2>&1; then
      printf '%s' "$public_url" | xclip -selection clipboard || true
      clip_msg="(Public URL copied to clipboard!)"
    fi
  fi
  echo ""
  echo "============================================================"
  echo "  Local Bridge : $local_url"
  if [[ -n "$public_url" ]]; then
    echo "  Public URL   : $public_url"
    [[ -n "$clip_msg" ]] && echo "  $clip_msg"
    echo "  Point your client's base URL at this address."
  elif [[ -n "$note" ]]; then
    echo "  Status       : $note"
  fi
  echo "  Press Ctrl+C or close this Terminal window to stop both."
  echo "============================================================"
  echo ""
}

BRIDGE_HOST="$(read_env_var BRIDGE_HOST 127.0.0.1)"
BRIDGE_PORT="$(read_env_var BRIDGE_PORT 8000)"
COMFYUI_URL="$(read_env_var COMFYUI_URL http://127.0.0.1:8188)"
TUNNEL_PROVIDER="$(read_env_var TUNNEL_PROVIDER auto | tr '[:upper:]' '[:lower:]')"
CLOUDFLARE_TUNNEL_TOKEN="$(read_env_var CLOUDFLARE_TUNNEL_TOKEN '')"
CLOUDFLARE_TUNNEL_NAME="$(read_env_var CLOUDFLARE_TUNNEL_NAME '')"
NGROK_DOMAIN="$(read_env_var NGROK_DOMAIN '')"
NGROK_DOMAIN="${NGROK_DOMAIN#https://}"
NGROK_DOMAIN="${NGROK_DOMAIN#http://}"
NGROK_DOMAIN="${NGROK_DOMAIN%/}"
LOCAL_BRIDGE_URL="http://${BRIDGE_HOST}:${BRIDGE_PORT}"

CLOUDFLARED_BIN="$(find_bin cloudflared /opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared "$HOME/.local/bin/cloudflared" || true)"
TAILSCALE_BIN="$(find_bin tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale /opt/homebrew/bin/tailscale /usr/local/bin/tailscale || true)"
NGROK_BIN="$(find_bin ngrok /opt/homebrew/bin/ngrok /usr/local/bin/ngrok "$HOME/.local/bin/ngrok" || true)"

if [[ "$TUNNEL_PROVIDER" == "auto" ]]; then
  if [[ -n "$CLOUDFLARE_TUNNEL_TOKEN" || -n "$CLOUDFLARE_TUNNEL_NAME" ]]; then
    TUNNEL_PROVIDER="cloudflare"
  elif [[ -n "$NGROK_DOMAIN" ]]; then
    TUNNEL_PROVIDER="ngrok"
  elif [[ -n "$CLOUDFLARED_BIN" ]]; then
    TUNNEL_PROVIDER="cloudflare"
  elif [[ -n "$TAILSCALE_BIN" ]]; then
    TUNNEL_PROVIDER="tailscale"
  elif [[ -n "$NGROK_BIN" ]]; then
    TUNNEL_PROVIDER="ngrok"
  else
    echo "[ERROR] No tunnel CLI found (cloudflared, tailscale, or ngrok). Install one or set TUNNEL_PROVIDER=none." >&2
    exit 1
  fi
fi

case "$TUNNEL_PROVIDER" in
  cloudflare)
    if [[ -z "$CLOUDFLARED_BIN" ]]; then
      echo "[ERROR] TUNNEL_PROVIDER=cloudflare selected, but cloudflared was not found (brew install cloudflared)." >&2
      exit 1
    fi
    ;;
  tailscale)
    if [[ -z "$TAILSCALE_BIN" ]]; then
      echo "[ERROR] TUNNEL_PROVIDER=tailscale selected, but tailscale CLI was not found." >&2
      exit 1
    fi
    ;;
  ngrok)
    if [[ -z "$NGROK_BIN" ]]; then
      echo "[ERROR] TUNNEL_PROVIDER=ngrok selected, but ngrok was not found (brew install ngrok)." >&2
      exit 1
    fi
    ;;
  none)
    ;;
  *)
    echo "[ERROR] Unsupported TUNNEL_PROVIDER '$TUNNEL_PROVIDER'. Use: auto, cloudflare, tailscale, ngrok, or none." >&2
    exit 1
    ;;
esac

COMFY_HOST_PORT="$("$VENV_PYTHON" - "$COMFYUI_URL" <<'PY'
import sys
from urllib.parse import urlsplit
p = urlsplit(sys.argv[1])
port = p.port or (443 if p.scheme == "https" else 80)
print(f"{p.hostname or '127.0.0.1'} {port}")
PY
)"
read -r COMFY_HOST COMFY_PORT <<<"$COMFY_HOST_PORT"
if port_is_listening "$COMFY_HOST" "$COMFY_PORT"; then
  echo "[OK] ComfyUI detected at $COMFYUI_URL"
else
  echo "[WARN] ComfyUI is not listening at $COMFYUI_URL yet. Open ComfyUI before generating images."
fi

UVICORN_PID=""
TUNNEL_PID=""
TMP_DIR="${TMPDIR:-/tmp}"
UVICORN_LOG="${TMP_DIR%/}/glint-studio-uvicorn.log"
TUNNEL_LOG="${TMP_DIR%/}/glint-studio-tunnel.log"

cleanup() {
  echo ""
  echo "Shutting down services..."
  if [[ -n "$TUNNEL_PID" ]] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
  if [[ -n "$UVICORN_PID" ]] && kill -0 "$UVICORN_PID" 2>/dev/null; then
    kill "$UVICORN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "$BRIDGE_DIR"

if port_is_listening "$BRIDGE_HOST" "$BRIDGE_PORT"; then
  echo "[WARN] Port $BRIDGE_PORT is already active at $LOCAL_BRIDGE_URL; keeping existing listener."
else
  rm -f "$UVICORN_LOG"
  echo "[1/2] Starting Python FastAPI bridge at $LOCAL_BRIDGE_URL ..."
  "$VENV_PYTHON" -m uvicorn app.main:app --host "$BRIDGE_HOST" --port "$BRIDGE_PORT" >"$UVICORN_LOG" 2>&1 &
  UVICORN_PID=$!

  READY=0
  for _ in $(seq 1 60); do
    if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
      echo "[ERROR] uvicorn exited early:" >&2
      cat "$UVICORN_LOG" >&2 || true
      exit 1
    fi
    if port_is_listening "$BRIDGE_HOST" "$BRIDGE_PORT"; then
      READY=1
      break
    fi
    sleep 0.3
  done

  if [[ "$READY" -ne 1 ]]; then
    echo "[ERROR] Timed out waiting for uvicorn on $LOCAL_BRIDGE_URL." >&2
    exit 1
  fi
  echo "[OK] Python bridge is listening on $LOCAL_BRIDGE_URL"
fi

if [[ "$TUNNEL_PROVIDER" == "none" ]]; then
  show_tunnel_ready "$LOCAL_BRIDGE_URL" "" "Local-only mode (TUNNEL_PROVIDER=none)"
  while true; do
    if [[ -n "$UVICORN_PID" ]] && ! kill -0 "$UVICORN_PID" 2>/dev/null; then
      echo "[ERROR] uvicorn stopped unexpectedly:" >&2
      tail -n 30 "$UVICORN_LOG" >&2 || true
      exit 1
    fi
    sleep 0.5
  done
fi

rm -f "$TUNNEL_LOG"
URL_REGEX=""
STATIC_NOTE=""

if [[ "$TUNNEL_PROVIDER" == "cloudflare" ]]; then
  if [[ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]]; then
    echo "[2/2] Starting named Cloudflare Tunnel via CLOUDFLARE_TUNNEL_TOKEN ..."
    "$CLOUDFLARED_BIN" tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" >"$TUNNEL_LOG" 2>&1 &
    STATIC_NOTE="Named Cloudflare Tunnel active"
  elif [[ -n "$CLOUDFLARE_TUNNEL_NAME" ]]; then
    echo "[2/2] Starting named Cloudflare Tunnel '$CLOUDFLARE_TUNNEL_NAME' ..."
    "$CLOUDFLARED_BIN" tunnel --no-autoupdate run "$CLOUDFLARE_TUNNEL_NAME" >"$TUNNEL_LOG" 2>&1 &
    STATIC_NOTE="Named Cloudflare Tunnel '$CLOUDFLARE_TUNNEL_NAME' active"
  else
    echo "[2/2] Starting Cloudflare Quick Tunnel -> $LOCAL_BRIDGE_URL ..."
    "$CLOUDFLARED_BIN" tunnel --no-autoupdate --url "$LOCAL_BRIDGE_URL" >"$TUNNEL_LOG" 2>&1 &
    URL_REGEX='https://[a-zA-Z0-9-]+\.trycloudflare\.com'
  fi
elif [[ "$TUNNEL_PROVIDER" == "tailscale" ]]; then
  echo "[2/2] Starting Tailscale Funnel -> $LOCAL_BRIDGE_URL ..."
  "$TAILSCALE_BIN" funnel "$LOCAL_BRIDGE_URL" >"$TUNNEL_LOG" 2>&1 &
  URL_REGEX='https://[a-zA-Z0-9.-]+\.ts\.net'
elif [[ "$TUNNEL_PROVIDER" == "ngrok" ]]; then
  if [[ -n "$NGROK_DOMAIN" ]]; then
    echo "[2/2] Starting ngrok static domain https://$NGROK_DOMAIN -> $LOCAL_BRIDGE_URL ..."
    "$NGROK_BIN" http "--domain=${NGROK_DOMAIN}" "$LOCAL_BRIDGE_URL" --log=stdout >"$TUNNEL_LOG" 2>&1 &
  else
    echo "[2/2] Starting ngrok tunnel -> $LOCAL_BRIDGE_URL ..."
    "$NGROK_BIN" http "$LOCAL_BRIDGE_URL" --log=stdout >"$TUNNEL_LOG" 2>&1 &
  fi
  URL_REGEX='https://[a-zA-Z0-9.-]+\.ngrok[a-zA-Z0-9.-]*'
fi
TUNNEL_PID=$!

ANNOUNCED=0
WAITED=0
while kill -0 "$TUNNEL_PID" 2>/dev/null; do
  if [[ "$ANNOUNCED" -eq 0 ]]; then
    if [[ -n "$STATIC_NOTE" ]]; then
      ANNOUNCED=1
      show_tunnel_ready "$LOCAL_BRIDGE_URL" "" "$STATIC_NOTE"
    elif [[ -f "$TUNNEL_LOG" ]]; then
      FOUND_URL="$(grep -Eo "$URL_REGEX" "$TUNNEL_LOG" | head -n 1 || true)"
      if [[ -n "$FOUND_URL" ]]; then
        ANNOUNCED=1
        show_tunnel_ready "$LOCAL_BRIDGE_URL" "$FOUND_URL" ""
      elif [[ "$TUNNEL_PROVIDER" == "tailscale" && "$WAITED" -ge 5 ]]; then
        TS_URL="$(get_tailscale_url "$TAILSCALE_BIN" || true)"
        if [[ -n "$TS_URL" ]]; then
          ANNOUNCED=1
          show_tunnel_ready "$LOCAL_BRIDGE_URL" "$TS_URL" ""
        fi
      elif [[ "$WAITED" -ge 60 ]]; then
        ANNOUNCED=1
        echo "[INFO] $TUNNEL_PROVIDER tunnel running (URL not captured yet; log follows):"
        cat "$TUNNEL_LOG" || true
      fi
    fi
  fi

  if [[ -n "$UVICORN_PID" ]] && ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    echo "[ERROR] uvicorn stopped unexpectedly:" >&2
    tail -n 30 "$UVICORN_LOG" >&2 || true
    exit 1
  fi
  WAITED=$((WAITED + 1))
  sleep 0.4
done

echo "[ERROR] $TUNNEL_PROVIDER exited unexpectedly:" >&2
tail -n 30 "$TUNNEL_LOG" >&2 || true
exit 1
