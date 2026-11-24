"""Load Python's ``ssl``, or say in one sentence why this host can't.

A DCC's Python links its ``_ssl`` against one OpenSSL. When the host lacks that library (Maya 2025 on
Ubuntu: ``ImportError: libssl.so.1.1``), ``import ssl`` fails with an error an artist can't act on. Every core
module that needs ``ssl`` gets it from ``require_ssl``, so that failure always carries the reason and the
package to install. This is the only module in ``symbiote_dcc`` that imports ``ssl`` at run time. Stdlib only.

The sentence shows a bounded detail of the error (the library it names, else a short head of it, never a
directory), because it reaches menus, dialogs and support screenshots. The full error goes to the log.
"""

from __future__ import annotations

import importlib
import logging
import re
from collections.abc import Callable
from types import ModuleType

# The OpenSSL a DCC's Python needs, by the library its ImportError names, and the package that provides it.
# Wording follows measurement: each clause was seen on that Rocky/RHEL major.
LIBRARY_PACKAGES: dict[str, str] = {
    "libssl.so.1.1": "openssl-libs on Rocky/RHEL 8, or compat-openssl11 on Rocky/RHEL 9",
    "libssl.so.3": "openssl3-libs from EPEL on Rocky/RHEL 8 (Rocky/RHEL 9 has it)",
}
# Where Rocky Linux and RHEL keep the system CA store (update-ca-trust writes it).
RHEL_CA_STORE = "/etc/pki/tls/certs/ca-bundle.crt"

# The most of an unknown error's head the sentence shows.
DETAIL_LIMIT = 80
logger = logging.getLogger(__name__)

_SUPPORTED = "Symbiote supports Rocky Linux and RHEL 8 and 9"
_UNSUPPORTED = "Other Linux distributions, Ubuntu among them, are not supported."


def error_detail(error: ImportError) -> str:
    """What the sentence shows of ``error``: the known library it names; else the text before its first ``": "``,
    without any directory and at most ``DETAIL_LIMIT`` characters."""
    text = str(error)
    library = _named_library(text)
    if library is not None:
        return library
    head = re.split(r"[\\/]", text.split(": ", 1)[0])[-1].strip()[:DETAIL_LIMIT]  # no directory, POSIX or Windows
    return head or type(error).__name__


def missing_ssl_reason(error: ImportError) -> str:
    """The sentence for an ``ssl`` that can't load: a bounded detail of the error, and the package that provides
    the library it names."""
    library = _named_library(str(error))
    install = f"; install {LIBRARY_PACKAGES[library]}." if library else "."
    return (
        f"Symbiote Chat can't start: this application's Python can't load its TLS library ({error_detail(error)}). "
        f"{_SUPPORTED}{install} {_UNSUPPORTED}"
    )


def _named_library(text: str) -> str | None:
    """The ``LIBRARY_PACKAGES`` library ``text`` names as a whole name: ``libssl.so.30`` is not ``libssl.so.3``."""
    return next((library for library in LIBRARY_PACKAGES if re.search(_whole(library), text)), None)


def _whole(library: str) -> str:
    return rf"(?<![\w.]){re.escape(library)}(?!\w|\.\w)"  # a sentence's full stop may follow


def missing_store_reason() -> str:
    """The sentence for a Linux host without the Rocky/RHEL CA store."""
    return (
        f"Symbiote Chat can't start: {RHEL_CA_STORE} doesn't exist. On Linux, Symbiote supports Rocky Linux and "
        f"RHEL 8 and 9, which keep their CA store there. {_UNSUPPORTED}"
    )


def require_ssl() -> ModuleType:
    """The ``ssl`` module; when it can't load, an ImportError whose message is ``missing_ssl_reason``."""
    try:
        import ssl
    except ImportError as error:
        _log(error)
        raise ImportError(missing_ssl_reason(error)) from error
    return ssl


def ssl_refusal(importer: Callable[[str], ModuleType] = importlib.import_module) -> str | None:
    """The reason ``ssl`` can't load on this host, or None when it loads."""
    try:
        importer("ssl")
    except ImportError as error:
        _log(error)
        return missing_ssl_reason(error)
    return None


def _log(error: ImportError) -> None:
    """The whole error, for whoever debugs the host; the sentence shows only ``error_detail``."""
    logger.error("Python's ssl module can't load: %s", error)
