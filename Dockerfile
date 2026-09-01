FROM golang:1.24-alpine AS alpaca-cli

RUN GOBIN=/out go install github.com/alpacahq/cli/cmd/alpaca@v0.0.14

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LASTSAFE_MODE=replay \
    LASTSAFE_DATABASE_PATH=/data/lastsafe.db

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY --from=alpaca-cli /out/alpaca /usr/local/bin/alpaca

RUN pip install --no-cache-dir .

RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001

EXPOSE 8000
CMD ["uvicorn", "lastsafe.main:app", "--host", "0.0.0.0", "--port", "8000"]
