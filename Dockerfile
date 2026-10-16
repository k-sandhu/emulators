# syntax=docker/dockerfile:1.7
# Multi-stage: uv resolves the locked runtime dependencies into /opt/venv, the final
# image is slim, non-root and has a healthcheck.
FROM python:3.13-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-default-groups --no-install-project
COPY src ./src
RUN uv sync --frozen --no-default-groups --no-editable

FROM python:3.13-slim-bookworm
LABEL org.opencontainers.image.source="https://github.com/k-sandhu/emulators" \
      org.opencontainers.image.description="High-fidelity SaaS API emulators for integration testing" \
      org.opencontainers.image.licenses="MIT"
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    EMULATORS_HOST=0.0.0.0 \
    EMULATORS_CERTS_DIR=/certs
RUN useradd --uid 10001 --create-home emulators && mkdir -p /certs && chown emulators /certs
COPY --from=build /opt/venv /opt/venv
USER 10001
EXPOSE 44080 44443 44101-44116
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"EMULATORS_PORT\",\"44080\")}/_emulator/health', timeout=3)" || exit 1
ENTRYPOINT ["emulators"]
CMD ["serve"]
