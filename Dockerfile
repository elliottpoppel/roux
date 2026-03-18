FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY server.py personal_auth.py CLAUDE.md ./

# Bake in seed data (places + taste profile)
RUN mkdir -p /data
COPY seed-data/ /data/

ENV ROUX_DATA_DIR=/data
ENV ROUX_TRANSPORT=streamable-http

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
