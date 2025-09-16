FROM mcr.microsoft.com/devcontainers/python:3

ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN apt -y update && apt -y upgrade

RUN curl -sSL https://install.python-poetry.org | python3 -
WORKDIR /workspaces/evse-controller

COPY ./poetry.lock ./pyproject.toml /workspaces/evse-controller/

RUN /root/.local/bin/poetry install --no-root
COPY . /workspaces/evse-controller/
RUN /root/.local/bin/poetry install

EXPOSE 5000
