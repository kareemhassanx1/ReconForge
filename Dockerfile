FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.lock /opt/reconforge/requirements-dev.lock
RUN python -m pip install --no-cache-dir -r /opt/reconforge/requirements-dev.lock
WORKDIR /work
