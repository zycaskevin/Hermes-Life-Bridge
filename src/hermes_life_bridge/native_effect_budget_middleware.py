"""Hermes execution middleware for AF's durable native-effect budget.

Only the private af.native-effect-budget-context.v1 execution context enables
this module. Ordinary Hermes turns are untouched. The Runtime sends the exact
request plus route/tool metadata to the private AF broker; the broker computes
the conservative quote from host-registered policy and atomically reserves it
before the provider/tool callback is allowed to execute.

This module never issues authority, prices a request, selects a Runtime/model,
implements research, or treats unknown cost as zero.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import socket
import stat
import threading
from pathlib import Path
from typing import Any, Callable

_SCHEMA = "af.native-effect-budget-context.v1"
_ALLOWED_TOOLS = frozenset({"web_search", "web_extract"})
_MAX_MESSAGE_BYTES = 1_048_576
_HALTED: set[str] = set()
_HALTED_LOCK = threading.RLock()


class NativeEffectBudgetMiddlewareError(RuntimeError):
    pass


class _DownstreamEffectError(BaseException):
    """Marks a provider/tool exception that occurred below the budget frame.

    The outer middleware handler converts its own validation/broker failures into
    ExecutionMiddlewareDenied. Downstream Runtime failures must instead keep their
    original type and message after the reservation is conservatively marked
    unknown, otherwise callers cannot distinguish provider failure from budget
    denial.
    """

    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


class _BudgetedIterator:
    """Delay settlement of a synchronous provider stream until terminal iteration.

    Creating a stream is not proof that its remote request completed. Clean
    exhaustion settles completed; iteration failure or explicit early close is
    conservatively unknown. Non-iterator attributes are forwarded to the provider
    stream for compatibility.
    """

    def __init__(self, iterator, context: dict[str, Any], effect_id: str, *, owner=None):
        self._delegate = iterator
        self._owner = iterator if owner is None else owner
        self._context = context
        self._effect_id = effect_id
        self._settled = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            value = next(self._delegate)
        except StopIteration:
            self._settle("completed")
            raise
        except BaseException:
            self._settle_unknown_preserving_downstream()
            raise
        return value

    def __getattr__(self, name: str):
        return getattr(self._owner, name)

    def _settle(self, outcome: str) -> None:
        if self._settled:
            return
        try:
            _finish(self._context, self._effect_id, outcome)
        except Exception as exc:
            _mark_local_halt(self._context)
            self._settled = True
            _deny(exc)
        self._settled = True

    def _settle_unknown_preserving_downstream(self) -> None:
        if self._settled:
            return
        try:
            _finish(self._context, self._effect_id, "unknown")
        except Exception:
            _mark_local_halt(self._context)
        self._settled = True

    def close(self):
        self._settle_unknown_preserving_downstream()
        owner_closer = getattr(self._owner, "close", None)
        delegate_closer = getattr(self._delegate, "close", None)
        if callable(owner_closer):
            result = owner_closer()
            if self._owner is self._delegate:
                return result
        if callable(delegate_closer):
            return delegate_closer()
        return None


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_request_not_canonical") from None
    if len(raw) > _MAX_MESSAGE_BYTES:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_request_too_large")
    return raw


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise NativeEffectBudgetMiddlewareError(code)
    try:
        int(value[7:], 16)
    except ValueError:
        raise NativeEffectBudgetMiddlewareError(code) from None
    return value


def _context() -> dict[str, Any] | None:
    try:
        from agent.execution_context import current_execution_context
        value = current_execution_context()
    except Exception:
        value = None
    if value is None or value.get("schema") != _SCHEMA:
        return None
    if set(value) != {"schema", "socketPath", "invocationRef", "quotePolicyRef"}:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_execution_context_invalid")
    invocation_ref = _sha(
        value.get("invocationRef"), "native_effect_invocation_ref_invalid")
    _sha(value.get("quotePolicyRef"), "native_effect_quote_policy_ref_invalid")
    socket_path = value.get("socketPath")
    if not isinstance(socket_path, str) or not socket_path:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_path_invalid")
    with _HALTED_LOCK:
        if invocation_ref in _HALTED:
            raise NativeEffectBudgetMiddlewareError(
                "native_effect_invocation_locally_halted")
    return value


def _socket_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or len(value.encode()) > 100:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_path_invalid")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_path_invalid")
    try:
        info = path.lstat()
    except OSError:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_unavailable") from None
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_identity_invalid")
    return path


def _read_response(connection: socket.socket) -> dict[str, Any]:
    data = bytearray()
    while len(data) <= _MAX_MESSAGE_BYTES:
        chunk = connection.recv(
            min(65536, _MAX_MESSAGE_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    if len(data) > _MAX_MESSAGE_BYTES or not data.endswith(b"\n"):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_response_invalid")
    try:
        value = json.loads(
            data,
            parse_constant=lambda _v: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_response_invalid") from None
    if not isinstance(value, dict):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_response_invalid")
    return value


def _broker(
    context: dict[str, Any], message: dict[str, Any]
) -> dict[str, Any]:
    path = _socket_path(context.get("socketPath"))
    raw = _canonical(message) + b"\n"
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(2.0)
    try:
        connection.connect(str(path))
        connection.sendall(raw)
        connection.shutdown(socket.SHUT_WR)
        response = _read_response(connection)
    except NativeEffectBudgetMiddlewareError:
        raise
    except (OSError, TimeoutError):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_broker_unavailable") from None
    finally:
        connection.close()
    if response.get("outcome") != "completed":
        reason = response.get("reason")
        stable = (
            reason
            if isinstance(reason, str) and reason.startswith("effect_")
            else "native_effect_budget_denied"
        )
        raise NativeEffectBudgetMiddlewareError(stable)
    if (
        response.get("quotePolicyRef") is not None
        and response.get("quotePolicyRef") != context.get("quotePolicyRef")
    ):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_quote_policy_mismatch")
    return response


def _effect_id(
    prefix: str, request: dict[str, Any], meta: dict[str, Any]
) -> str:
    identity = {
        "apiRequestId": str(meta.get("api_request_id") or ""),
        "toolCallId": str(meta.get("tool_call_id") or ""),
        "callRole": str(meta.get("call_role") or ""),
        "apiCallCount": meta.get("api_call_count"),
        "retryCount": meta.get("retry_count"),
        "auxiliaryTask": str(meta.get("auxiliary_task") or ""),
        "auxiliaryPhysicalAttempt": meta.get("auxiliary_physical_attempt"),
        "request": _digest(request),
    }
    return (
        prefix + ":"
        + hashlib.sha256(_canonical(identity)).hexdigest()[:48]
    )


def _deny(exc: BaseException):
    try:
        from hermes_cli.middleware import ExecutionMiddlewareDenied
    except Exception:
        ExecutionMiddlewareDenied = NativeEffectBudgetMiddlewareError
    if isinstance(exc, ExecutionMiddlewareDenied):
        raise exc
    code = (
        str(exc)
        if isinstance(exc, NativeEffectBudgetMiddlewareError)
        else "native_effect_budget_failure"
    )
    raise ExecutionMiddlewareDenied(code) from None


def _reserve(
    context: dict[str, Any],
    *,
    effect_id: str,
    kind: str,
    request: dict[str, Any],
    effect_meta: dict[str, Any],
) -> None:
    response = _broker(
        context,
        {
            "action": "reserve",
            "invocationRef": context["invocationRef"],
            "effectId": effect_id,
            "kind": kind,
            "request": request,
            "effectMeta": effect_meta,
        },
    )
    if response.get("quotePolicyRef") not in {
        None, context.get("quotePolicyRef")
    }:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_quote_policy_mismatch")


def _finish(
    context: dict[str, Any], effect_id: str, outcome: str
) -> None:
    response = _broker(
        context,
        {
            "action": "finish",
            "invocationRef": context["invocationRef"],
            "effectId": effect_id,
            "effectOutcome": outcome,
        },
    )
    snapshot = response.get("snapshot")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("quotePolicyRef") != context.get("quotePolicyRef")
    ):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_quote_policy_mismatch")


def _mark_local_halt(context: dict[str, Any]) -> None:
    with _HALTED_LOCK:
        _HALTED.add(str(context.get("invocationRef") or ""))


def _provider_meta(meta: dict[str, Any]) -> dict[str, Any]:
    provider, model = meta.get("provider"), meta.get("model")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(model, str)
        or not model
    ):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_model_route_missing")
    return {"provider": provider, "model": model}


def on_llm_execution(
    request: dict[str, Any], next_call: Callable, **meta
):
    try:
        context = _context()
    except BaseException as exc:
        _deny(exc)
    if context is None:
        return next_call(request)
    try:
        if not isinstance(request, dict):
            raise NativeEffectBudgetMiddlewareError(
                "native_effect_model_request_invalid")
        kind = (
            "auxiliary_model"
            if str(meta.get("call_role") or "").startswith("auxiliary:")
            else "model"
        )
        effect_id = _effect_id(
            "aux" if kind == "auxiliary_model" else "model",
            request,
            meta,
        )
        _reserve(
            context,
            effect_id=effect_id,
            kind=kind,
            request=request,
            effect_meta=_provider_meta(meta),
        )
        try:
            result = next_call(request)
        except BaseException as exc:
            try:
                _finish(context, effect_id, "unknown")
            except Exception:
                _mark_local_halt(context)
            raise _DownstreamEffectError(exc) from exc
        if inspect.isawaitable(result):
            async def await_result():
                try:
                    value = await result
                except BaseException:
                    try:
                        _finish(context, effect_id, "unknown")
                    except Exception:
                        _mark_local_halt(context)
                    raise
                try:
                    _finish(context, effect_id, "completed")
                except Exception as exc:
                    _mark_local_halt(context)
                    _deny(exc)
                return value
            return await_result()
        if bool(meta.get("streaming")):
            try:
                iterator = iter(result)
            except TypeError:
                iterator = None
            if iterator is not None:
                return _BudgetedIterator(
                    iterator, context, effect_id, owner=result)
        try:
            _finish(context, effect_id, "completed")
        except Exception as exc:
            _mark_local_halt(context)
            _deny(exc)
        return result
    except _DownstreamEffectError as exc:
        raise exc.original
    except BaseException as exc:
        _deny(exc)



def _tool_route(tool_name: str) -> str:
    try:
        if tool_name == "web_search":
            from tools.web_tools import _get_search_backend
            route = _get_search_backend()
        elif tool_name == "web_extract":
            from tools.web_tools import _get_extract_backend
            route = _get_extract_backend()
        else:
            raise NativeEffectBudgetMiddlewareError(
                "native_effect_tool_unapproved")
    except NativeEffectBudgetMiddlewareError:
        raise
    except Exception:
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_tool_route_unavailable") from None
    if (not isinstance(route, str) or not route or len(route) > 128
            or any(ord(ch) < 33 or ord(ch) > 126 for ch in route)):
        raise NativeEffectBudgetMiddlewareError(
            "native_effect_tool_route_invalid")
    return route


def on_tool_execution(
    tool_name: str,
    args: dict[str, Any],
    next_call: Callable,
    **meta,
):
    try:
        context = _context()
    except BaseException as exc:
        _deny(exc)
    if context is None:
        return next_call(args)
    try:
        if (
            not isinstance(tool_name, str)
            or tool_name not in _ALLOWED_TOOLS
            or not isinstance(args, dict)
        ):
            raise NativeEffectBudgetMiddlewareError(
                "native_effect_tool_unapproved")
        route = _tool_route(tool_name)
        from agent.execution_context import scoped_tool_route_pins

        request = {"tool": tool_name, "args": args}
        effect_id = _effect_id("tool", request, meta)
        _reserve(
            context,
            effect_id=effect_id,
            kind="tool",
            request=request,
            effect_meta={"tool": tool_name, "route": route},
        )
        try:
            with scoped_tool_route_pins({tool_name: route}):
                result = next_call(args)
        except BaseException as exc:
            try:
                _finish(context, effect_id, "unknown")
            except Exception:
                _mark_local_halt(context)
            raise _DownstreamEffectError(exc) from exc

        if inspect.isawaitable(result):
            async def await_result():
                try:
                    with scoped_tool_route_pins({tool_name: route}):
                        value = await result
                except BaseException:
                    try:
                        _finish(context, effect_id, "unknown")
                    except Exception:
                        _mark_local_halt(context)
                    raise
                try:
                    _finish(context, effect_id, "completed")
                except Exception as exc:
                    _mark_local_halt(context)
                    _deny(exc)
                return value
            return await_result()

        try:
            _finish(context, effect_id, "completed")
        except Exception as exc:
            _mark_local_halt(context)
            _deny(exc)
        return result
    except _DownstreamEffectError as exc:
        raise exc.original
    except BaseException as exc:
        _deny(exc)


def reset_local_halts_for_tests() -> None:
    """Test-only helper; production reconciliation never calls this."""
    with _HALTED_LOCK:
        _HALTED.clear()
