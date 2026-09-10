import logging
import sys

import structlog

from app.core.config import settings


def configure_logging() -> None:
    """JSON logs in production, human-readable ones locally.

    Docker captures stdout into json-file logs capped at 10m x 3 (see the stack
    file), so structured lines here are what `docker logs` and any future shipper
    both read.
    """
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO)

    renderer: structlog.types.Processor = (
        structlog.dev.ConsoleRenderer()
        if settings.environment == "development"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
