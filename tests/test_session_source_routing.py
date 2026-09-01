from dataclasses import dataclass
from enum import Enum
from hermes_life_bridge.routing import normalize_session_source, RouteStore

class Platform(Enum):
    FEISHU="feishu"

@dataclass
class SessionSourceLike:
    platform: Platform
    chat_id: str
    thread_id: str|None=None
    message_id: str|None=None
    def to_dict(self):
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "message_id": self.message_id,
        }

def test_feishu_dm_target():
    r=normalize_session_source(SessionSourceLike(Platform.FEISHU,"oc_123",message_id="om_1"))
    assert r.platform=="feishu"
    assert r.target=="feishu:oc_123"

def test_thread_target():
    r=normalize_session_source(SessionSourceLike(Platform.FEISHU,"oc_123","th_1"))
    assert r.target=="feishu:oc_123:th_1"

def test_attribute_fallback():
    class S:
        platform=Platform.FEISHU
        chat_id="oc_attr"
        thread_id=None
        message_id="om_attr"
    assert normalize_session_source(S()).target=="feishu:oc_attr"

def test_repr_string_rejected():
    r=normalize_session_source("SessionSource(platform=<Platform.FEISHU: 'feishu'>, chat_id='oc_1')")
    assert r.target==""

def test_private_route_store(tmp_path):
    path=tmp_path/"route.json"
    RouteStore(str(path)).save(normalize_session_source(SessionSourceLike(Platform.FEISHU,"oc_private")))
    assert (path.stat().st_mode & 0o777)==0o600
    assert RouteStore(str(path)).load()["target"]=="feishu:oc_private"
