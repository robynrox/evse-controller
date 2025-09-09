FROM python:3

ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN curl -sSL https://install.python-poetry.org | python3 -
WORKDIR /c
COPY poetry.lock pyproject.toml /c/
RUN /root/.local/bin/poetry install --no-root
COPY . /c/
RUN /root/.local/bin/poetry install

EXPOSE 5000

ENTRYPOINT /usr/local/bin/python -m evse_controller.app
