#!/bin/bash
set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$GEMINI_API_KEY" ]; then
  echo "Error: GEMINI_API_KEY not set. Copy .env.example to .env and add your key."
  exit 1
fi

venv/bin/uvicorn main:app --reload --port 8000
