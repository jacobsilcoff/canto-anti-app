#!/bin/bash
set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$GEMINI_API_KEY" ]; then
  echo "Warning: GEMINI_API_KEY not set — translation will fail, but UI is usable."
fi

if [ -f "${DB_PATH:-data/cards.db}" ]; then
  cp "${DB_PATH:-data/cards.db}" "${DB_PATH:-data/cards.db}.bak"
  echo "DB backed up to ${DB_PATH:-data/cards.db}.bak"
fi

venv/bin/uvicorn main:app --reload --port 8000
