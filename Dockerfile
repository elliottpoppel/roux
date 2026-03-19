FROM python:3.11-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY server.py personal_auth.py db.py CLAUDE.md ./

# Create data directory for places database and taste profile.
# For personal deployments, put your places.json and taste-profile.md
# in seed-data/ before building — they'll be baked into the image.
RUN mkdir -p /data

ENV ROUX_DATA_DIR=/data
ENV ROUX_TRANSPORT=streamable-http

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
