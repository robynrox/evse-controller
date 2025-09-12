FROM python:3

ARG DEBIAN_FRONTEND=noninteractive
ENV POETRY_VIRTUALENVS_CREATE=false \
    POETRY_NO_INTERACTION=1 \
    POETRY_CACHE_DIR='/var/cache/pypoetry'

RUN curl -sSL https://install.python-poetry.org | python3 -
WORKDIR /app

COPY poetry.lock pyproject.toml /app/

RUN /root/.local/bin/poetry install --no-root
COPY . /app/
RUN /root/.local/bin/poetry install

EXPOSE 5000

RUN ln -fs /usr/share/zoneinfo/Europe/London /etc/localtime

ENTRYPOINT ["/usr/local/bin/python"]
CMD ["-m", "evse_controller.app"]