FROM python:3.12-slim

ARG UV_VERSION=0.12.1
RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && useradd --create-home --uid 10001 inboxpilot

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"

COPY . /app
RUN uv sync --locked --no-dev \
    && mkdir -p /app/data/private \
    && chown -R inboxpilot:inboxpilot /app

USER inboxpilot
EXPOSE 8765

CMD ["uvicorn", "inbox_agent.web.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
