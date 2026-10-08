#!/usr/bin/env bash
# Все тесты проекта: tools/test.sh — на Mac или на Pi (нужен только python3, без пакетов).
set -uo pipefail
cd "$(dirname "$0")/.."

failed=0
for dir in webcam alerts zapret; do
  printf '%-8s ' "$dir"
  if out=$(cd "$dir" && python3 -m unittest discover -s tests 2>&1); then
    echo "$out" | grep -E '^Ran ' | tr -d '\n'; echo " — OK"
  else
    echo "ОШИБКИ:"; echo "$out" | tail -30; failed=1
  fi
done
find . -name __pycache__ -type d -prune -exec rm -rf {} +
exit $failed
