# log-lib-py

Python logging library for Gratheon services.

## Features

- Colored console logger for `info`, `warn`, `error`, and `debug`
- Optional Loki target with the same default URL and label conventions as `log-lib` and `log-lib-go`
- Automatic `file:line` capture for regular log calls
- Enriched exception logging with stack traces, cause chain, and code frame output in `ENV_ID=dev`
- Package-level API for backward-compatible simple usage
- Context binding helpers for request correlation such as `req_id` and `user_id`
- Global uncaught exception handling via `sys.excepthook` and `threading.excepthook`
- Fire-and-forget Loki delivery that never blocks the hot path

## Install

```bash
pip install gratheon-log-lib
```

## Usage

```python
from gratheon_log_lib import LoggerConfig, LokiConfig, create_logger

logger = create_logger(
    LoggerConfig(
        log_level="info",
        loki=LokiConfig(
            enabled=True,
            url="http://loki:3100/loki/api/v1/push",
            service="user-cycle",
            labels={"team": "platform"},
        ),
    )
)

logger.info("service started")
logger.warn("memory getting low", {"available": "120MB"})

try:
    raise RuntimeError("boom")
except RuntimeError as exc:
    logger.error(exc, {"job": "sync"})
    logger.error_enriched("job failed", exc, {"job": "sync"})
```

## Package-level API

```python
import gratheon_log_lib as logger

logger.configure()
logger.info("service started")
logger.bind_context(req_id="abc-123", user_id="42")
logger.error("request failed")
logger.clear_context()
```

## Loki behavior

- If `loki.enabled=False`, Loki is disabled.
- If `loki` is omitted, Loki still defaults to enabled and uses:
  - `LOKI_URL` or `http://loki:3100/loki/api/v1/push`
  - `SERVICE_NAME`, `COMPOSE_SERVICE`, or the current folder name for the service label
- `debug` logs are console-only and are not sent to Loki.

## Development mode

Set `ENV_ID=dev` to enable:

- code frames for exceptions
- Loki transport error diagnostics
- default log level `debug`

## API

- `create_logger(config: LoggerConfig | None = None) -> GratheonLogger`
- `configure(config: LoggerConfig | None = None) -> GratheonLogger`
- `get_logger() -> GratheonLogger`
- `info(message, meta=None)`
- `warn(message, meta=None)`
- `debug(message, meta=None)`
- `error(message_or_exception, meta=None)`
- `error_enriched(message, error, meta=None)`
- `fatal(message_or_exception, meta=None)`
- `bind_context(**fields)`
- `clear_context()`
- `request_context(**fields)`
