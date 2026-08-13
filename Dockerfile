# One image, four agents. Which agent a container becomes is decided by the
# command it is started with (`serve coordinator`, `serve analyst`, …) and by
# the environment injected at run time. No credentials are ever baked in.

# --- build stage -------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Dependencies resolve from the project metadata alone, so this layer is only
# rebuilt when the metadata changes — not on every source edit.
COPY pyproject.toml README.md ./
COPY src ./src
# Tracing is opt-in at run time (TELEMETRY_ENABLED), but the dependency ships in
# the image so enabling it never needs a rebuild.
RUN pip install --no-cache-dir ".[telemetry]"

# --- runtime stage -----------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000 \
    LOG_FORMAT=json

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent

COPY --from=builder /opt/venv /opt/venv

USER agent
WORKDIR /home/agent

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=5 \
    CMD python -c "import os,sys,urllib.request; \
sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/health\", timeout=4).status == 200 else 1)"

ENTRYPOINT ["research-desk"]
CMD ["serve", "coordinator"]
