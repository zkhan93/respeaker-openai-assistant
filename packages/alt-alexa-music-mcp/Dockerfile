FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        mpv \
        ffmpeg \
        ca-certificates \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.0 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy only metadata first so dep install caches independently of source.
COPY pyproject.toml ./
RUN uv venv /opt/venv \
    && . /opt/venv/bin/activate \
    && uv pip install --no-deps -e . \
    && uv pip install \
        "fastmcp>=2.10.0" \
        "httpx>=0.27.0" \
        "pydantic>=2.7.0" \
        "pydantic-settings>=2.4.0" \
        "rapidfuzz>=3.9.0" \
        "yt-dlp>=2024.10.0"

ENV PATH="/opt/venv/bin:$PATH"

COPY src ./src
RUN . /opt/venv/bin/activate && uv pip install --no-deps -e .

# mpv IPC socket and downloads land on host-mounted volumes.
RUN mkdir -p /run/mpv /library
VOLUME ["/library", "/run/mpv"]

EXPOSE 7801

ENTRYPOINT ["tini", "--"]
CMD ["alt-alexa-music-mcp"]
