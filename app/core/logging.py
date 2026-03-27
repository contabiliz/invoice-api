"""
Structured logging via structlog with JSON output.

PII scrubbing: CNPJ, CPF, financial amounts, and filenames are never logged.
Only document_id, batch_id, request_id, and doc_type are bound per request.
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

# Context variables bound per-request (set in middleware)
request_id_var: ContextVar[str] = ContextVar("request_id", default="")
user_id_var: ContextVar[str] = ContextVar("user_id", default="")
batch_id_var: ContextVar[str] = ContextVar("batch_id", default="")

# Fields that may contain PII — scrub their values before output
_PII_FIELDS = frozenset({
    "filename",
    "original_filename",
    "cnpj",
    "cpf",
    "documento",
    "valor",
    "valor_total",
    "file_size",
    "chave_acesso",
})


def _scrub_pii(
    logger: Any,
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    for key in list(event_dict):
        if key.lower() in _PII_FIELDS:
            event_dict[key] = "[REDACTED]"
    return event_dict


def _inject_context(
    logger: Any,
    method: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    if rid := request_id_var.get():
        event_dict["request_id"] = rid
    if uid := user_id_var.get():
        event_dict["user_id"] = uid
    if bid := batch_id_var.get():
        event_dict["batch_id"] = bid
    return event_dict


def configure_logging(json_logs: bool = True) -> None:
    """
    Configure structlog in standalone mode (PrintLoggerFactory).

    Do NOT mix with stdlib ProcessorFormatter — that is a separate
    integration pattern incompatible with PrintLoggerFactory.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            _scrub_pii,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure stdlib logging for uvicorn / third-party libraries
    logging.basicConfig(
        format="%(levelname)s\t%(name)s\t%(message)s",
        level=logging.INFO,
        stream=sys.stdout,
        force=True,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
