"""
Logging setup for run.py: console + a rotating file in data/logs/, with login tokens
masked (old bookmarks may still carry ?token=… in URLs).
"""
import logging
import re

from app.config import LOGS_DIR

_TOKEN = re.compile(r"(token=)[^&\s\"']+")


class RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(_TOKEN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _TOKEN.sub(r"\1***", record.msg)
        return True


def uvicorn_log_config() -> dict:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"redact": {"()": RedactTokens}},
        "formatters": {
            "console": {"()": "uvicorn.logging.DefaultFormatter", "fmt": "%(levelprefix)s %(message)s"},
            "access": {"()": "uvicorn.logging.AccessFormatter",
                       "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'},
            "file": {"format": fmt},
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "console", "filters": ["redact"]},
            "access": {"class": "logging.StreamHandler", "formatter": "access", "filters": ["redact"]},
            "file": {"class": "logging.handlers.RotatingFileHandler", "formatter": "file", "filters": ["redact"],
                     "filename": str(LOGS_DIR / "server.log"), "maxBytes": 5 * 1024 * 1024, "backupCount": 3,
                     "encoding": "utf-8"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access", "file"], "level": "INFO", "propagate": False},
            "invictus": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        },
    }
