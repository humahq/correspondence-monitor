FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 CORMON_HOME=/app
WORKDIR /app

COPY pyproject.toml ./
COPY cormon ./cormon
RUN pip install --no-cache-dir .

COPY config ./config
COPY studies ./studies
COPY demo ./demo

# data/ (database, raw messages, attachments) and secrets/ are mounted volumes.
VOLUME ["/app/data", "/app/secrets"]
EXPOSE 8080
CMD ["cormon", "run"]
