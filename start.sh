#!/bin/bash
set -e

# Load .env if it exists
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

if [ -z "$GROQ_API_KEY" ]; then
  echo "Error: GROQ_API_KEY not set. Copy .env.example to .env and add your key."
  echo "Get a free key at: https://console.groq.com"
  exit 1
fi

venv/bin/uvicorn main:app --reload --port 8000
