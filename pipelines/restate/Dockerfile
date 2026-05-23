FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
        libcairo2 libpango-1.0-0 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY shared /app/shared
COPY --from=garmin-fetch . /app/garmin-fetch
COPY restate /app/restate

RUN sed -i 's|path = "/projects/garmin-fetch"|path = "/app/garmin-fetch"|' \
        /app/shared/pyproject.toml

WORKDIR /app/restate
RUN sed -i 's|path = "../shared"|path = "/app/shared"|' pyproject.toml

RUN uv sync --no-cache
RUN uv run playwright install --with-deps chromium

ENV PYTHONUNBUFFERED=1
ENV APPROVE_PORT=8001
EXPOSE 9080 8001

CMD ["uv", "run", "pipeline-restate-app"]
