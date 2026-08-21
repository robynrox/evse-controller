FROM mcr.microsoft.com/devcontainers/python:3.13

ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN apt -y update && apt -y upgrade
RUN python3 -m pip install --no-cache-dir poetry

WORKDIR /workspaces/evse-controller
COPY ./poetry.lock ./pyproject.toml .

RUN poetry install --no-root
COPY . .
RUN poetry install

EXPOSE 5000
