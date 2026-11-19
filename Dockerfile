FROM rust:1-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN python3 -m venv /buildenv && /buildenv/bin/pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12-slim-bookworm
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl pytest
WORKDIR /check
COPY tests tests
RUN python -m pytest tests/ -v
