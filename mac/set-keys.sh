#!/bin/bash
# Paste keys into secrets.env without them showing on screen. Press Return on an empty line to skip one.
cd "$(dirname "$0")/.."
f="secrets.env"
[ -f "$f" ] || cp secrets.env.example "$f"
for k in THREADS_TOKEN THREADS_APP_SECRET INSTAGRAM_TOKEN TIKTOK_CLIENT_KEY TIKTOK_CLIENT_SECRET; do
  printf "Paste %s (Return to skip): " "$k"
  IFS= read -rs v; echo
  v="$(printf '%s' "$v" | tr -d '[:space:]"')"
  [ -z "$v" ] && { echo "  skipped"; continue; }
  grep -v "^$k=" "$f" > "$f.tmp"; echo "$k=$v" >> "$f.tmp"; mv "$f.tmp" "$f"; chmod 600 "$f"
  echo "  saved ($k, ${#v} characters)"
done
echo "Done."
