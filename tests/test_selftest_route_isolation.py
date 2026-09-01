from hermes_life_bridge.routing import HermesRoute, RouteStore, is_delivery_route

def test_synthetic_route_cannot_overwrite_real_route(tmp_path):
    path=tmp_path/"route.json"
    RouteStore(str(path)).save(HermesRoute("feishu","oc_REAL"))
    before=path.read_text()
    RouteStore(str(path)).save(HermesRoute("hlb-selftest"))
    assert path.read_text()==before

def test_only_real_chat_routes_are_delivery_routes():
    assert is_delivery_route(HermesRoute("feishu","oc_REAL"))
    assert not is_delivery_route(HermesRoute("hlb-selftest"))
    assert not is_delivery_route(HermesRoute("gateway"))
    assert not is_delivery_route(HermesRoute("cli"))
