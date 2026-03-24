#!/usr/bin/env bash
# mirror.sh — Generate a static mirror of https://southeasttravel.com.ph into docs/
# Dependencies: GNU wget
# Usage: bash scripts/mirror.sh

set -euo pipefail

SITE_URL="https://southeasttravel.com.ph"
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/docs"
USER_AGENT="Mozilla/5.0 (compatible; StaticMirrorBot/1.0)"

echo "==> Cleaning output directory: ${OUTPUT_DIR}"
rm -rf "${OUTPUT_DIR}"
mkdir -p "${OUTPUT_DIR}"

echo "==> Mirroring ${SITE_URL} ..."
wget \
  --recursive \
  --level=inf \
  --page-requisites \
  --convert-links \
  --adjust-extension \
  --no-host-directories \
  --directory-prefix="${OUTPUT_DIR}" \
  --domains=southeasttravel.com.ph \
  --span-hosts=off \
  --no-parent \
  --wait=1 \
  --random-wait \
  --user-agent="${USER_AGENT}" \
  --reject-regex="(logout|signout|wp-login|xmlrpc)" \
  --timeout=30 \
  --tries=3 \
  --quiet \
  --show-progress \
  "${SITE_URL}" || true

echo "==> Creating .nojekyll"
touch "${OUTPUT_DIR}/.nojekyll"

# Create a basic 404 page if one wasn't downloaded
if [ ! -f "${OUTPUT_DIR}/404.html" ]; then
  echo "==> Creating default docs/404.html"
  cat > "${OUTPUT_DIR}/404.html" <<'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>404 – Page Not Found</title>
  <style>
    body { font-family: sans-serif; text-align: center; padding: 4rem 1rem; }
    h1   { font-size: 3rem; margin-bottom: 0.5rem; }
    p    { color: #555; }
    a    { color: #0070f3; text-decoration: none; }
    a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>404</h1>
  <p>The page you're looking for doesn't exist.</p>
  <p><a href="/">← Back to home</a></p>
</body>
</html>
EOF
fi

echo "==> Mirror complete. Files written to: ${OUTPUT_DIR}"
