"""Pure validation for the one explicit downstream provider entrypoint."""

from __future__ import annotations

import re

from .contracts import DownstreamFailure, DownstreamReasonCode


_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", re.ASCII)
_FACTORY = re.compile(r"^[A-Za-z_]\w*$", re.ASCII)


def validate_provider_entrypoint(entrypoint: str) -> tuple[str, str]:
    """Return the module and factory names, without importing either one."""

    if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
        raise DownstreamFailure(DownstreamReasonCode.INVALID_ENTRYPOINT)
    module_name, factory_name = entrypoint.split(":", 1)
    if not _MODULE.fullmatch(module_name) or not _FACTORY.fullmatch(factory_name):
        raise DownstreamFailure(DownstreamReasonCode.INVALID_ENTRYPOINT)
    return module_name, factory_name


__all__ = ["validate_provider_entrypoint"]
