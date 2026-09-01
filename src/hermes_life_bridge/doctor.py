from __future__ import annotations
from pathlib import Path
import socket
from .config import BridgeConfig
from .hermes_api import HermesApiClient
from .trace import BridgeTracer
from .routing import RouteStore


def _probe_unix(path: str, timeout: float) -> dict:
    result={"path":path,"exists":Path(path).exists(),"connect":False}
    if result["exists"]:
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
                s.settimeout(timeout); s.connect(path); result["connect"]=True
        except Exception as exc:
            result["error"]=type(exc).__name__
    return result


def run_doctor(config: BridgeConfig | None = None) -> dict:
    config=config or BridgeConfig.from_env(); tracer=BridgeTracer(config.trace_path)
    result={
        "runtime_socket":_probe_unix(config.runtime_socket,config.connect_timeout_seconds),
        "cognition_socket":_probe_unix(config.cognition_socket,config.connect_timeout_seconds),
        "contact_socket":_probe_unix(config.contact_socket,config.connect_timeout_seconds),
        "hermes_api":{"base_url":config.hermes_api_base_url,"healthy":False,"key_loaded":bool(config.hermes_api_key)},
        "contact":{"delivery_enabled":config.contact_delivery_enabled,"target":config.contact_target},
        "trace":{"path":config.trace_path,"exists":Path(config.trace_path).exists(),"writable_parent":False,"last_stage":None,"last_status":None,"last_hook":None},
        "overall":"DEGRADED",
    }
    route=RouteStore(config.route_path).load()
    result["route"]={
        "ready":bool(route and route.get("target")),
        "platform":(route or {}).get("platform"),
        "chat_id_present":bool((route or {}).get("chat_id")),
        "thread_id_present":bool((route or {}).get("thread_id")),
        "target_redacted":bool(route and route.get("target")),
    }
    parent=Path(config.trace_path).parent
    try:
        parent.mkdir(parents=True,exist_ok=True); result["trace"]["writable_parent"]=parent.is_dir()
    except Exception: pass
    tail=tracer.tail(1)
    if tail:
        last=tail[-1]; result["trace"].update(last_stage=last.get("stage"),last_status=last.get("status"),last_hook=last.get("hook"))
    try:
        HermesApiClient(config).health(); result["hermes_api"]["healthy"]=True
    except Exception as exc:
        result["hermes_api"]["error"]=type(exc).__name__
    if result["runtime_socket"]["connect"] and result["cognition_socket"]["connect"] and result["contact_socket"]["connect"] and result["hermes_api"]["healthy"] and result["trace"]["writable_parent"]:
        result["overall"]="HEALTHY" if result["trace"]["last_status"] != "fail" else "DEGRADED"
    return result
