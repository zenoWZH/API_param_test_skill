"""Explicit reviewed handler registrations; definitions never import code."""
from __future__ import annotations

import hashlib
import inspect
import marshal
import re
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .common import IntegrityError, PlanValidationError, digest_json, require_id


def _portable_code(code):
    """Root/App and isolated checkouts must bind identical source identically."""
    return code.replace(co_filename="<handler>", co_consts=tuple(_portable_code(item) if isinstance(item, types.CodeType) else item for item in code.co_consts))


@dataclass(frozen=True)
class _Handler:
    function: Callable
    version: str
    declared_digest: str | Callable | None

    def binding(self) -> dict:
        fn = self.function
        declared_digest = self.declared_digest() if callable(self.declared_digest) else self.declared_digest
        if declared_digest is not None and (not isinstance(declared_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_digest)):
            raise PlanValidationError("Handler dependency digest provider must return a lowercase SHA256")
        try:
            source_path = inspect.getsourcefile(fn)
            source_digest = hashlib.sha256(Path(source_path).read_bytes()).hexdigest() if source_path else None
        except (OSError, TypeError):
            source_digest = None
        code = getattr(fn, "__code__", None)
        if code is None and self.declared_digest is None:
            raise PlanValidationError("Native or callable-object handlers require an explicit digest")
        # The declared package digest augments, rather than replaces, executable binding.
        material = {
            "module": getattr(fn, "__module__", ""),
            "qualname": getattr(fn, "__qualname__", type(fn).__qualname__),
            "source_digest": source_digest,
            "code_digest": hashlib.sha256(marshal.dumps(_portable_code(code))).hexdigest() if code else None,
            "declared_digest": declared_digest,
        }
        return {"version": self.version, "digest": digest_json(material)}


class HandlerRegistry:
    """Register trusted Python functions before compiling JSON definitions.

    ``register('text.execute', handler, version='1')`` returns the handler, and
    decorator syntax is also supported. A handler takes ``(context, inputs)``.
    Bindings cover the function bytecode and its complete source module, plus an
    optional explicit package/dependency digest supplied by the adapter owner.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, _Handler] = {}

    def register(self, handler_id: str, function: Callable | None = None, *, version: str = "1", digest: str | Callable | None = None):
        require_id(handler_id, "handler ID")
        require_id(version, "handler version")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", handler_id):
            raise PlanValidationError("Invalid handler ID")
        if digest is not None and not callable(digest) and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise PlanValidationError("Handler digest must be a lowercase SHA256")
        def add(fn: Callable):
            if handler_id in self._handlers:
                raise PlanValidationError(f"Duplicate handler ID: {handler_id}")
            if not callable(fn):
                raise PlanValidationError(f"Handler is not callable: {handler_id}")
            item = _Handler(fn, version, digest)
            item.binding()
            self._handlers[handler_id] = item
            return fn
        return add if function is None else add(function)

    def bindings(self) -> dict:
        return {name: entry.binding() for name, entry in sorted(self._handlers.items())}

    def resolve(self, handler_id: str, binding: dict | None = None) -> Callable:
        if handler_id not in self._handlers:
            raise PlanValidationError(f"Unregistered handler: {handler_id}")
        entry = self._handlers[handler_id]
        if binding is not None and entry.binding() != binding:
            raise IntegrityError(f"Handler version or digest drift: {handler_id}")
        return entry.function

    def verify(self, bindings: dict) -> None:
        for name, binding in bindings.items():
            self.resolve(name, binding)
