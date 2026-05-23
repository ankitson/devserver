FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN uv sync --no-cache

ENV PORT=8000
EXPOSE 8000
CMD ["uv", "run", "mock-bank"]
