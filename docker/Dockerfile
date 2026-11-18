# The development image. Build context: docker/ (nothing else is copied; src/ is mounted at /work at run time).
#   uv      tool stage: runs `make lock` for Python (uv.lock); the final image builds on it
#   node    tool stage: runs `make lock` for the frontend (package-lock.json)
#   vendor  installs the frontend libraries from package-lock.json
#   e2e     Chromium + Playwright for the end-to-end test only (make e2e); never part of the app image
#   gh      the GitHub CLI (pinned, checksum-verified) for make pr / pr-status only; never part of the app image
#   final   Python dependencies from uv.lock + the frontend libraries from vendor
# certs/ may hold extra CA certificates (*.crt) for a TLS-inspecting proxy; it is empty on a normal machine.

FROM python:3.14-slim AS uv
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    UV_NATIVE_TLS=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.11.32

FROM node:22-slim AS node
COPY certs/ /tmp/certs/
RUN cat /tmp/certs/*.crt > /usr/local/share/extra-ca.pem 2>/dev/null; true
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/extra-ca.pem

FROM node AS vendor
WORKDIR /opt/vendor
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    || { echo "docker/package-lock.json does not match docker/package.json: run 'make lock', then commit both"; exit 1; }

FROM mcr.microsoft.com/playwright:v1.63.0-noble AS e2e
COPY certs/ /tmp/certs/
RUN cat /tmp/certs/*.crt > /usr/local/share/extra-ca.pem 2>/dev/null; true
ENV NODE_EXTRA_CA_CERTS=/usr/local/share/extra-ca.pem
WORKDIR /opt/e2e
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
ENV NODE_PATH=/opt/e2e/node_modules

FROM debian:bookworm-slim AS gh
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl git \
    && update-ca-certificates && rm -rf /var/lib/apt/lists/*
# Bump GH_VERSION to update; the build stops if the tarball does not match the release's checksum file.
ARG GH_VERSION=2.62.0
RUN set -eu; arch="$(dpkg --print-architecture)"; name="gh_${GH_VERSION}_linux_${arch}"; \
    base="https://github.com/cli/cli/releases/download/v${GH_VERSION}"; cd /tmp; \
    curl -fsSLO "$base/$name.tar.gz"; curl -fsSLO "$base/gh_${GH_VERSION}_checksums.txt"; \
    grep " $name.tar.gz\$" "gh_${GH_VERSION}_checksums.txt" | sha256sum -c -; \
    tar -xzf "$name.tar.gz" -C /usr/local/bin --strip-components=2 "$name/bin/gh"; \
    rm -f "$name.tar.gz" "gh_${GH_VERSION}_checksums.txt"; gh --version
ENV HOME=/tmp GH_NO_UPDATE_NOTIFIER=1 GH_PROMPT_DISABLED=1
WORKDIR /work
ENTRYPOINT ["gh"]

FROM uv AS final
WORKDIR /opt/project
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project \
    || { echo "docker/uv.lock does not match docker/pyproject.toml: run 'make lock', then commit both"; exit 1; }
COPY --from=vendor /opt/vendor/node_modules /opt/vendor/node_modules
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/work/src VENDOR_DIR=/opt/vendor/node_modules \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg HOME=/tmp
WORKDIR /work
EXPOSE 8300
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8300"]
