from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import json
import os
import pathlib
import sys
import threading
import time
import traceback
import types
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple, Type


RESET = "\033[0m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
PURPLE = "\033[35m"
GREY = "\033[90m"
CYAN = "\033[36m"

LOG_LEVELS = {
    "debug": 0,
    "info": 1,
    "warn": 2,
    "error": 3,
}

_context_fields: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "gratheon_log_lib_context", default={}
)
_logger_lock = threading.RLock()
_global_logger = None  # type: Optional["GratheonLogger"]
_project_root = pathlib.Path.cwd()
_handlers_installed = False


@dataclasses.dataclass
class LokiConfig:
    url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    tenant_id: Optional[str] = None
    service: Optional[str] = None
    labels: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None


@dataclasses.dataclass
class LoggerConfig:
    otlp: Optional["OtlpConfig"] = None
    loki: Optional[LokiConfig] = None
    log_level: Optional[str] = None


@dataclasses.dataclass
class OtlpConfig:
    endpoint: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    service: Optional[str] = None
    resource_attributes: Optional[Dict[str, str]] = None
    enabled: Optional[bool] = None


@dataclasses.dataclass
class _ResolvedLokiConfig:
    url: str
    service: str
    labels: Dict[str, str]
    username: Optional[str] = None
    password: Optional[str] = None
    tenant_id: Optional[str] = None
    enabled: bool = True


@dataclasses.dataclass
class _ResolvedOtlpConfig:
    endpoint: str
    service: str
    headers: Dict[str, str]
    resource_attributes: Dict[str, str]
    logger: Any = None
    enabled: bool = True


def bind_context(**fields: Any) -> None:
    current = dict(_context_fields.get())
    current.update(fields)
    _context_fields.set(current)


def clear_context() -> None:
    _context_fields.set({})


def get_context() -> Dict[str, Any]:
    return dict(_context_fields.get())


@contextlib.contextmanager
def request_context(**fields: Any) -> Iterator[None]:
    current = dict(_context_fields.get())
    updated = dict(current)
    updated.update(fields)
    token = _context_fields.set(updated)
    try:
        yield
    finally:
        _context_fields.reset(token)


def _env_or_default(key: str, fallback: str) -> str:
    value = os.getenv(key, "").strip()
    return value or fallback


def _detect_service_name() -> str:
    for key in ("SERVICE_NAME", "COMPOSE_SERVICE", "npm_package_name"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return pathlib.Path.cwd().name or "service"


def _resolve_log_level(level: Optional[str]) -> str:
    if level:
        return _normalize_log_level(level)
    env_level = os.getenv("LOG_LEVEL", "").strip()
    if env_level:
        return _normalize_log_level(env_level)
    if os.getenv("ENV_ID", "").lower() == "dev":
        return "debug"
    return "info"


def _normalize_log_level(level: str) -> str:
    normalized = level.strip().lower()
    if normalized in ("trace", "debug"):
        return "debug"
    if normalized in ("warning", "warn"):
        return "warn"
    if normalized in ("fatal", "panic", "error"):
        return "error"
    return "info"


def _resolve_loki_config(config: Optional[LoggerConfig]) -> Optional[_ResolvedLokiConfig]:
    explicit = config.loki if config else None
    if explicit is None and not os.getenv("LOKI_URL", "").strip():
        return None
    enabled = True if explicit is None or explicit.enabled is None else explicit.enabled
    if not enabled:
        return None

    service = explicit.service.strip() if explicit and explicit.service else _detect_service_name()
    url = explicit.url.strip() if explicit and explicit.url else _env_or_default(
        "LOKI_URL", "http://loki:3100/loki/api/v1/push"
    )
    labels = {
        "service": service,
        "env": _env_or_default("ENV_ID", "unknown"),
        "logger": "log-lib-py",
    }
    if explicit and explicit.labels:
        labels.update(explicit.labels)

    return _ResolvedLokiConfig(
        url=url,
        service=service,
        labels=labels,
        username=explicit.username if explicit else None,
        password=explicit.password if explicit else None,
        tenant_id=explicit.tenant_id if explicit else None,
        enabled=True,
    )


def _parse_key_value_list(raw: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _normalize_otlp_logs_endpoint(endpoint: str) -> str:
    endpoint = endpoint.strip().rstrip("/")
    if endpoint.endswith("/v1/logs"):
        return endpoint
    return f"{endpoint}/v1/logs"


def _resolve_otlp_config(config: Optional[LoggerConfig]) -> Optional[_ResolvedOtlpConfig]:
    explicit = config.otlp if config else None
    endpoint = (
        (explicit.endpoint.strip() if explicit and explicit.endpoint else "")
        or os.getenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )
    enabled = bool(endpoint) if explicit is None or explicit.enabled is None else explicit.enabled
    if not enabled or not endpoint:
        return None

    service = (
        (explicit.service.strip() if explicit and explicit.service else "")
        or os.getenv("OTEL_SERVICE_NAME", "").strip()
        or _detect_service_name()
    )
    resource_attributes = {
        "service.name": service,
        "service.namespace": "gratheon",
        "deployment.environment.name": _env_or_default("ENV_ID", "unknown"),
    }
    resource_attributes.update(_parse_key_value_list(os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")))
    if explicit and explicit.resource_attributes:
        resource_attributes.update(explicit.resource_attributes)

    headers: Dict[str, str] = {}
    if os.getenv("HYPERDX_API_KEY", "").strip():
        headers["authorization"] = os.getenv("HYPERDX_API_KEY", "").strip()
    headers.update(_parse_key_value_list(os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")))
    headers.update(_parse_key_value_list(os.getenv("OTEL_EXPORTER_OTLP_LOGS_HEADERS", "")))
    if explicit and explicit.headers:
        headers.update(explicit.headers)

    otlp_logger = None
    try:
        from opentelemetry._logs import SeverityNumber  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        provider = LoggerProvider(resource=Resource.create(resource_attributes))
        provider.add_log_record_processor(
            SimpleLogRecordProcessor(
                OTLPLogExporter(endpoint=_normalize_otlp_logs_endpoint(endpoint), headers=headers)
            )
        )
        otlp_logger = provider.get_logger("gratheon-log-lib-py", "1.0.0")
    except Exception as exc:
        if os.getenv("ENV_ID", "").lower() == "dev":
            print(f"[log-lib-py] Failed to initialize OTLP exporter: {exc}", file=sys.stderr)

    return _ResolvedOtlpConfig(
        endpoint=_normalize_otlp_logs_endpoint(endpoint),
        service=service,
        headers=headers,
        resource_attributes=resource_attributes,
        logger=otlp_logger,
    )


def _safe_jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(item, depth + 1) for item in value]
    if isinstance(value, BaseException):
        return {
            "type": value.__class__.__name__,
            "message": str(value),
        }
    if dataclasses.is_dataclass(value):
        return _safe_jsonable(dataclasses.asdict(value), depth + 1)
    if hasattr(value, "__dict__"):
        return _safe_jsonable(vars(value), depth + 1)
    return repr(value)


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(_safe_jsonable(value), ensure_ascii=True, sort_keys=True, default=repr)


def _severity_number(level: str) -> int:
    if level == "debug":
        return 5
    if level == "warn":
        return 13
    if level == "error":
        return 17
    return 9


def _otlp_value(value: Any) -> Dict[str, Any]:
    if value is None:
        return {"stringValue": ""}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": _safe_message(value)}


def _otlp_attributes(fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"key": str(key), "value": _otlp_value(value)} for key, value in fields.items()]


def _safe_message(value: Any) -> str:
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, str):
        return value
    try:
        return _safe_json_dumps(value)[:2000]
    except Exception:
        return str(value)


def _relativize_path(file_path: str) -> str:
    try:
        candidate = pathlib.Path(file_path).resolve()
        try:
            return str(candidate.relative_to(_project_root))
        except ValueError:
            return str(candidate)
    except Exception:
        return file_path


def _call_site(skip: int = 2) -> Optional[str]:
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return None
    while frame:
        file_name = frame.f_code.co_filename
        if "gratheon_log_lib" not in file_name:
            relative = _relativize_path(file_name)
            return f"{relative}:{frame.f_lineno}"
        frame = frame.f_back
    return None


def _exception_frame(exc: BaseException) -> Tuple[Optional[str], Optional[int]]:
    tb = exc.__traceback__
    if tb is None:
        return None, None
    extracted = traceback.extract_tb(tb)
    if not extracted:
        return None, None
    frame = extracted[-1]
    return _relativize_path(frame.filename), frame.lineno


def _format_stack(exc: BaseException, limit: int = 5) -> str:
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    merged = "".join(lines).strip().splitlines()
    trimmed = merged[:limit]
    return "\n".join(f"{GREY}{line}{RESET}" for line in trimmed)


def _build_code_frame(exc: BaseException, context_lines: int = 2) -> str:
    tb = exc.__traceback__
    if tb is None:
        return ""
    extracted = traceback.extract_tb(tb)
    if not extracted:
        return ""
    culprit = extracted[-1]
    file_path = pathlib.Path(culprit.filename)
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    start = max(1, culprit.lineno - context_lines)
    end = min(len(lines), culprit.lineno + context_lines)
    output = []  # type: List[str]
    for line_no in range(start, end + 1):
        prefix = f"{RED}>{RESET}" if line_no == culprit.lineno else " "
        output.append(f"{prefix} {line_no:4d} | {lines[line_no - 1]}")
    return "\n".join(output)


def _cause_chain(exc: BaseException) -> List[str]:
    chain = []  # type: List[str]
    current = exc.__cause__ or exc.__context__
    seen = set()  # type: Set[int]
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(f"{current.__class__.__name__}: {current}")
        current = current.__cause__ or current.__context__
    return chain


class GratheonLogger:
    def __init__(self, config: Optional[LoggerConfig] = None) -> None:
        self._config = config or LoggerConfig()
        self._log_level = _resolve_log_level(self._config.log_level)
        self._loki = _resolve_loki_config(self._config)
        self._otlp = _resolve_otlp_config(self._config)

    def info(self, message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        self._emit("info", _safe_message(message), meta or {})

    def warn(self, message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        self._emit("warn", _safe_message(message), meta or {})

    def debug(self, message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        self._emit("debug", _safe_message(message), meta or {}, persist=False)

    def error(self, message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        if isinstance(message, BaseException):
            self._emit_exception(message, meta or {})
            return
        self._emit("error", _safe_message(message), meta or {})

    def fatal(self, message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
        self.error(message, meta)
        raise SystemExit(1)

    def error_enriched(
        self, message: str, error: Any, meta: Optional[Dict[str, Any]] = None
    ) -> None:
        payload = meta or {}
        if isinstance(error, BaseException):
            self._emit_exception(error, payload, prefix=f"{message}: ")
            return
        self._emit("error", f"{message}: {_safe_message(error)}", payload)

    def _should_log(self, level: str) -> bool:
        return LOG_LEVELS[level] >= LOG_LEVELS.get(self._log_level, LOG_LEVELS["info"])

    def _emit(
        self,
        level: str,
        message: str,
        meta: Dict[str, Any],
        *,
        persist: bool = True,
        stacktrace: str = "",
        file_location: Optional[str] = None,
    ) -> None:
        if not self._should_log(level):
            return

        merged_meta = {**get_context(), **meta}
        self._write_console(level, message, merged_meta, file_location or _call_site(4))
        if persist and level != "debug":
            self._store_in_otlp(level, message, merged_meta, stacktrace)
            self._store_in_loki(level, message, merged_meta, stacktrace)

    def _emit_exception(
        self, exc: BaseException, meta: Dict[str, Any], prefix: str = ""
    ) -> None:
        file_name, line_no = _exception_frame(exc)
        location = f"{file_name}:{line_no}" if file_name and line_no else None
        merged_meta = {**meta}
        message = f"{prefix}{exc}"
        stacktrace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._emit(
            "error",
            message,
            {
                "name": exc.__class__.__name__,
                "stack": stacktrace,
                "causeChain": _cause_chain(exc),
                **merged_meta,
            },
            stacktrace=stacktrace,
            file_location=location,
        )
        print(_format_stack(exc))
        if os.getenv("ENV_ID", "").lower() == "dev":
            code_frame = _build_code_frame(exc)
            if code_frame:
                print(f"\n{CYAN}Code frame:{RESET}\n{code_frame}\n")

    def _write_console(
        self, level: str, message: str, meta: Dict[str, Any], file_location: Optional[str]
    ) -> None:
        now = time.strftime("%H:%M:%S", time.localtime())
        timestamp = f"{BLUE}{now}{RESET}"
        level_colored = {
            "info": f"{GREEN}info{RESET}",
            "warn": f"{YELLOW}warn{RESET}",
            "error": f"{RED}error{RESET}",
            "debug": f"{GREY}debug{RESET}",
        }[level]
        message_colored = f"{GREY}{message}{RESET}" if level == "debug" else message
        meta_text = ""
        if meta:
            meta_text = f" {PURPLE}{_safe_json_dumps(meta)}{RESET}"
        location_text = f" {GREY}{file_location}{RESET}" if file_location else ""
        print(f"{timestamp} [{level_colored}]: {message_colored}{meta_text}{location_text}")

    def _store_in_loki(
        self, level: str, message: str, meta: Dict[str, Any], stacktrace: str = ""
    ) -> None:
        if self._loki is None or not self._loki.enabled:
            return

        payload = self._safe_payload_for_test(level, message, meta, stacktrace)
        thread = threading.Thread(
            target=self._push_loki,
            args=(payload,),
            name="gratheon-log-lib-loki",
            daemon=True,
        )
        thread.start()

    def _store_in_otlp(
        self, level: str, message: str, meta: Dict[str, Any], stacktrace: str = ""
    ) -> None:
        if self._otlp is None or not self._otlp.enabled:
            return
        payload = self._otlp_payload_for_test(level, message, meta, stacktrace)
        thread = threading.Thread(
            target=self._push_otlp,
            args=(payload,),
            name="gratheon-log-lib-otlp",
            daemon=True,
        )
        thread.start()

    def _otlp_payload_for_test(
        self, level: str, message: str, meta: Dict[str, Any], stacktrace: str = ""
    ) -> bytes:
        if self._otlp is None:
            raise RuntimeError("OTLP is not configured")

        attributes = {**meta, "log.logger": "log-lib-py"}
        if stacktrace:
            attributes["stacktrace"] = stacktrace
        trace_id = attributes.pop("trace_id", attributes.pop("traceId", None))
        span_id = attributes.pop("span_id", attributes.pop("spanId", None))
        record: Dict[str, Any] = {
            "timestamp": time.time_ns(),
            "observed_timestamp": time.time_ns(),
            "severity_text": level.upper(),
            "severity_number": _severity_number(level),
            "body": message,
            "attributes": _safe_jsonable(attributes),
        }
        if trace_id:
            try:
                record["trace_id"] = int(str(trace_id), 16)
            except ValueError:
                attributes["trace_id"] = str(trace_id)
        if span_id:
            try:
                record["span_id"] = int(str(span_id), 16)
            except ValueError:
                attributes["span_id"] = str(span_id)
        return json.dumps(record, ensure_ascii=True).encode("utf-8")

    def _push_otlp(self, payload: bytes) -> None:
        assert self._otlp is not None
        try:
            if self._otlp.logger is not None:
                record = json.loads(payload.decode("utf-8"))
                self._otlp.logger.emit(**record)
        except Exception:
            if os.getenv("ENV_ID", "").lower() == "dev":
                print("[log-lib-py] Failed to export log to OTLP", file=sys.stderr)

    def _safe_payload_for_test(
        self, level: str, message: str, meta: Dict[str, Any], stacktrace: str = ""
    ) -> bytes:
        if self._loki is None:
            raise RuntimeError("Loki is not configured")

        line = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "level": level,
            "service": self._loki.service,
            "message": message,
            "meta": _safe_jsonable(meta),
            "stacktrace": stacktrace or "",
        }
        body = {
            "streams": [
                {
                    "stream": self._loki.labels,
                    "values": [[str(time.time_ns()), _safe_json_dumps(line)[:120000]]],
                }
            ]
        }
        return json.dumps(body, ensure_ascii=True).encode("utf-8")

    def _push_loki(self, payload: bytes) -> None:
        assert self._loki is not None
        request = urllib.request.Request(
            self._loki.url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self._loki.tenant_id:
            request.add_header("X-Scope-OrgID", self._loki.tenant_id)
        if self._loki.username or self._loki.password:
            import base64

            token = f"{self._loki.username or ''}:{self._loki.password or ''}".encode("utf-8")
            request.add_header("Authorization", f"Basic {base64.b64encode(token).decode('ascii')}")

        try:
            with urllib.request.urlopen(request, timeout=2):
                pass
        except (urllib.error.URLError, TimeoutError, OSError):
            if os.getenv("ENV_ID", "").lower() == "dev":
                print("[log-lib-py] Failed to persist log to Loki", file=sys.stderr)


def create_logger(config: Optional[LoggerConfig] = None) -> GratheonLogger:
    return GratheonLogger(config)


def configure(config: Optional[LoggerConfig] = None) -> GratheonLogger:
    global _global_logger
    instance = create_logger(config)
    with _logger_lock:
        _global_logger = instance
    _install_global_handlers()
    return instance


def get_logger() -> GratheonLogger:
    global _global_logger
    with _logger_lock:
        if _global_logger is None:
            _global_logger = create_logger()
            _install_global_handlers()
        return _global_logger


def info(message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().info(message, meta)


def warn(message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().warn(message, meta)


def debug(message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().debug(message, meta)


def error(message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().error(message, meta)


def error_enriched(message: str, err: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().error_enriched(message, err, meta)


def fatal(message: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    get_logger().fatal(message, meta)


def _install_global_handlers() -> None:
    global _handlers_installed
    if _handlers_installed:
        return

    previous_sys_hook = sys.excepthook

    def excepthook(
        exc_type: Type[BaseException], exc: BaseException, tb: Optional[types.TracebackType]
    ) -> None:
        print("=== UNCAUGHT EXCEPTION ===", file=sys.stderr)
        try:
            get_logger().error(exc)
        finally:
            previous_sys_hook(exc_type, exc, tb)

    sys.excepthook = excepthook

    previous_thread_hook = getattr(threading, "excepthook", None)

    def thread_excepthook(args: Any) -> None:
        print("=== UNCAUGHT THREAD EXCEPTION ===", file=sys.stderr)
        try:
            get_logger().error(args.exc_value)
        finally:
            if previous_thread_hook is not None:
                previous_thread_hook(args)

    if previous_thread_hook is not None:
        threading.excepthook = thread_excepthook

    _handlers_installed = True
