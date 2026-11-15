# Netproof: FastAPI server + ngspice in one container (serves web/ too).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ngspice \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.2

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Dependencies only (the app runs from source via PYTHONPATH below).
RUN uv sync --frozen --no-dev --extra gemini --no-install-project
COPY backend ./backend
COPY web ./web

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    NETPROOF_AI_RATE_LIMIT=10 \
    NETPROOF_AI_DAILY_CAP=200
EXPOSE 8000
CMD ["sh", "-c", "uvicorn netproof.server:app --host 0.0.0.0 --port ${PORT}"]
