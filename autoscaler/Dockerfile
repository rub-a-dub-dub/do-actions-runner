FROM python:3.12-slim

# Invalidate cache with real build layer change
ENV AUTOSCALER_VERSION=2.0.2

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY autoscaler/pyproject.toml .
COPY autoscaler/autoscaler.py .

RUN uv pip install --system --no-cache requests

CMD ["python", "-u", "autoscaler.py"]
