FROM python:3.13-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY . .
RUN uv sync --no-dev --no-editable

EXPOSE 8080

CMD ["sh", "-c", "uv run uvicorn src.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
