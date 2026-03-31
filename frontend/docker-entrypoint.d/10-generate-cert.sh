#!/bin/sh
set -eu

CERT_DIR="/etc/nginx/certs"
CERT_FILE="$CERT_DIR/fullchain.pem"
KEY_FILE="$CERT_DIR/privkey.pem"

mkdir -p "$CERT_DIR"

if [ "${ALLOW_SELF_SIGNED_TLS:-False}" != "True" ]; then
  if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "[tls] missing TLS certificate files in $CERT_DIR"
    echo "[tls] mount real certificates or set ALLOW_SELF_SIGNED_TLS=True for local dev only"
    exit 1
  fi
  exit 0
fi

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
  echo "[tls] generating self-signed development certificate"
  openssl req -x509 -nodes -days 365 \
    -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out "$CERT_FILE" \
    -subj "/CN=localhost"
fi
