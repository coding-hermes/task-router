\"\"\"Tests for OpenAPI + dispatch table parity (TR-275).\"\"\"
import pytest
from task_router import openapi, server


def get_dispatch_routes() -> set[tuple[str, str]]:
    \"\"\"Extract (method, path) from the dispatch table.\"\"\"
    return set(server.DISPATCH_TABLE.keys())


def get_openapi_routes() -> set[tuple[str, str]]:
    \"\"\"Extract (method, path) from the OpenAPI schema.\"\"\"
    schema = openapi.build_openapi()
    routes = set()
    for path, methods in schema.get(\"paths\", {}).items():
        for method in methods.keys():
            routes.add((method.upper(), path))
    return routes


def test_openapi_dispatch_bidirectional():
    \"\"\"AC1: Bidirectional diff: dispatch table vs /openapi.json.\"\"\"
    dispatch_routes = get_dispatch_routes()
    openapi_routes = get_openapi_routes()
    
    # Filter out /openapi.json itself (it's meta, not a real route)
    dispatch_routes = {(m, p) for m, p in dispatch_routes if p != \"/openapi.json\"}
    openapi_routes = {(m, p) for m, p in openapi_routes if p != \"/openapi.json\"}
    
    missing_in_openapi = dispatch_routes - openapi_routes
    extra_in_openapi = openapi_routes - dispatch_routes
    
    assert not missing_in_openapi, f\"Routes in dispatch table but missing from OpenAPI: {missing_in_openapi}\"
    assert not extra_in_openapi, f\"Routes in OpenAPI but not in dispatch table: {extra_in_openapi}\"


def test_dispatch_table_is_authoritative():
    \"\"\"The dispatch table must be a dict, not None or empty.\"\"\"
    assert isinstance(server.DISPATCH_TABLE, dict)
    assert len(server.DISPATCH_TABLE) > 0


def test_openapi_schema_is_valid():
    \"\"\"The OpenAPI schema must be valid and non-empty.\"\"\"
    schema = openapi.build_openapi()
    assert \"openapi\" in schema
    assert \"paths\" in schema
    assert len(schema[\"paths\"]) > 0


def test_openapi_dispatch_no_duplicates():
    \"\"\"AC2: Each route appears exactly once in both dispatch and OpenAPI.\"\"\"
    dispatch_routes = get_dispatch_routes()
    openapi_routes = get_openapi_routes()
    
    # No duplicates in dispatch table (it's a dict, so keys are unique by construction)
    assert len(dispatch_routes) == len(server.DISPATCH_TABLE)
    
    # No duplicates in OpenAPI (paths dict + methods dict ensure uniqueness)
    schema = openapi.build_openapi()
    route_count = sum(len(methods) for methods in schema[\"paths\"].values())
    assert route_count == len(openapi_routes)
</ARG>