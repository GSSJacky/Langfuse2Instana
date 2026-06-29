FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

COPY config.yaml.example config.yaml

EXPOSE 8000

ENTRYPOINT ["langfuse2instana"]
CMD ["-c", "/app/config.yaml"]
