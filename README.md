# log-lib-py

Python logging library for Gratheon services with colored console output and optional OTLP log export to ClickStack/OpenTelemetry collectors.

## Version

`1.0.0` is the ClickStack/OTLP major release.

Breaking behavior change: Loki is no longer enabled implicitly. Configure `OTEL_EXPORTER_OTLP_ENDPOINT` for ClickStack logs, or set `LOKI_URL`/`LokiConfig` if a service still needs Loki during migration.

## Features

- Colored console logger for `info`, `warn`, `error`, and `debug`
- Optional OTLP HTTP JSON log export
- Optional Loki compatibility transport
- Automatic `file:line` capture
- Enriched exception logging with stack traces, cause chains, and dev-mode code frames
- Context binding helpers for request correlation
- Global uncaught exception handling

## ClickStack Setup

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=models-bee-detector
OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=dev,service.namespace=gratheon
```

The library sends logs to `/v1/logs`, so either a collector base endpoint or a full logs endpoint works.

## Usage

```python
from gratheon_log_lib import LoggerConfig, OtlpConfig, create_logger

logger = create_logger(
    LoggerConfig(
        log_level="info",
        otlp=OtlpConfig(
            endpoint="http://localhost:4318",
            service="models-bee-detector",
            resource_attributes={"deployment.environment.name": "dev"},
        ),
    )
)

logger.info("service started")
logger.warn("slow inference", {"duration_ms": 830})

try:
    raise RuntimeError("boom")
except RuntimeError as exc:
    logger.error_enriched("job failed", exc, {"job": "sync"})
```

Trace correlation is supported when metadata includes `trace_id`/`traceId` and `span_id`/`spanId`.
