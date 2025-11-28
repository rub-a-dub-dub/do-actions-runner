FROM python:3.12-slim

# Build version: 3 (force fresh build)
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY autoscaler/pyproject.toml .
COPY autoscaler/autoscaler.py .

RUN uv pip install --system --no-cache requests

CMD ["python", "-u", "autoscaler.py"]
