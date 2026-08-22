FROM mcr.microsoft.com/devcontainers/python:3.12

ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN apt -y update && apt -y upgrade
RUN python3 -m pip install --no-cache-dir poetry

# Ensure Poetry cache directory exists and has correct permissions
RUN mkdir -p /var/cache/pypoetry && \
    chown -R vscode:vscode /var/cache/pypoetry

# Switch to vscode user for all Poetry operations
USER vscode
WORKDIR /workspaces/evse-controller

COPY . .
RUN poetry -v install

EXPOSE 5000
