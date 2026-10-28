# Find-a-Line: one image for both processes of the public site (compose.yaml runs
# `findaline.py web --public` and `findaline.py worker --public` from it).
#
# Base images are pinned by tag and digest (the digest of the multi-platform index). To update
# them: pick the new tags, look up their digests with
#   docker buildx imagetools inspect python:3.13.<n>-slim-trixie
#   docker buildx imagetools inspect ghcr.io/astral-sh/uv:<version>
# (the "Digest:" line), change both ARGs below, then `docker compose build --pull` and run the
# CI job (`docker` in .github/workflows/ci.yml). Both stages use the same Python image, so the
# virtual environment built in the first one runs unchanged in the second.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie@sha256:1151df520c42ebd86b3b81ad2929a8cf2a01ef0d7745eb72322bf48b26c82d78
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0

FROM ${UV_IMAGE} AS uv

# Build: the locked dependencies (wheels only, no compiler in the image) and the scripts
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/findaline/.venv
WORKDIR /opt/findaline
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project --no-build
COPY . .
# The scripts' bytecode, valid whatever the files' times after the copy (the root filesystem is
# read-only at run time, so nothing could be cached later)
RUN python -m compileall -q -l --invalidation-mode unchecked-hash .

# Runtime: Python and the environment, no uv, no build tools
FROM ${PYTHON_IMAGE}
# uid 10001; its home, numba's cache and matplotlib's settings live in the data volume (the root
# filesystem is read-only). /data is created here owned by the user, so a new named volume takes
# that owner when it is first mounted.
RUN groupadd --gid 10001 findaline \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /data/home --shell /usr/sbin/nologin findaline \
    && mkdir -p /data/home \
    && chown -R 10001:10001 /data
COPY --from=build /opt/findaline /opt/findaline
ENV PATH=/opt/findaline/.venv/bin:$PATH \
    VIRTUAL_ENV=/opt/findaline/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FINDALINE_DATA=/data \
    HOME=/data/home \
    NUMBA_CACHE_DIR=/data/home/.cache/numba \
    MPLCONFIGDIR=/data/home/.config/matplotlib \
    MPLBACKEND=Agg
WORKDIR /opt/findaline
USER 10001:10001
EXPOSE 8765
# No HEALTHCHECK here: the image runs the worker too, which serves no port; compose.yaml checks
# the web service (its Host header must be the public host, TrustedHostMiddleware refuses others)
ENTRYPOINT ["python", "findaline.py"]
CMD ["--help"]
