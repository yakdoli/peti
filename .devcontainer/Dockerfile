FROM mcr.microsoft.com/devcontainers/python:1-3.11-bookworm

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*
