FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY worker ./worker
COPY README.md ./
RUN uv sync --frozen --no-dev

EXPOSE 8765
CMD ["uv", "run", "uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "8765"]
