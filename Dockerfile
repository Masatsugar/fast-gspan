FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends cmake g++ make && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir ".[dev]"
RUN python -m fast_gspan build
RUN python -m pytest tests/ -v
