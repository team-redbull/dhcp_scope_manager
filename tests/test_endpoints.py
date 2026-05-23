"""API endpoint tests using FastAPI TestClient with mocked service layer."""
import json
from unittest.mock import patch
import pytest
from httpx import ASGITransport, AsyncClient
from app.main import app
from app.models import DhcpExclusion, DhcpScopePayload
from app.errors import PowerShellError

pytestmark = pytest.mark.asyncio


class AsyncTestClient:
    async def request(self, method: str, path: str, **kwargs):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, **kwargs)

    async def get(self, path: str, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs):
        return await self.request("PUT", path, **kwargs)

    async def delete(self, path: str, **kwargs):
        return await self.request("DELETE", path, **kwargs)


client = AsyncTestClient()


def _error(body):
    return body["error"]


def _make_scope_dict(**overrides):
    base = {
        "scopeName": "Cluster-A Management",
        "network": "10.20.30.0",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.30.100",
        "endRange": "10.20.30.200",
        "leaseDurationDays": 8,
        "description": "Cluster A management network",
        "gateway": "10.20.30.1",
        "dnsServers": ["10.0.0.53", "10.0.0.54"],
        "dnsDomain": "lab.local",
        "exclusions": [{"startAddress": "10.20.30.1", "endAddress": "10.20.30.99"}],
        "failover": None,
    }
    base.update(overrides)
    return base


def _make_scope(**overrides):
    return DhcpScopePayload(**_make_scope_dict(**overrides))


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

async def test_get_existing_scope():
    scope = _make_scope()
    with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
        r = await client.get("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 200
    data = r.json()
    assert data["network"] == "10.20.30.0"
    assert data["leaseDurationDays"] == 8


async def test_get_missing_scope():
    with patch(
        "app.services.scope_service.assemble_scope_state",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "No DHCP scope found", 1),
    ):
        r = await client.get("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 404
    err = _error(r.json())
    assert err["code"] == "SCOPE_NOT_FOUND"
    assert "10.20.30.0" in err["message"]


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

async def test_post_create_new_scope():
    created = _make_scope()
    with patch("app.services.scope_service.create_scope", return_value=created):
        r = await client.post("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 200
    assert r.json()["network"] == "10.20.30.0"


async def test_post_bare_scopes_route_not_found():
    """POST /scopes (without scope_id) must not exist — all writes go through Git/Crossplane."""
    r = await client.post("/api/v1/scopes", json=_make_scope_dict())
    assert r.status_code == 405  # Method Not Allowed — path exists (GET /scopes) but not POST


async def test_post_idempotent_existing():
    """POST on existing scope must return 200, never 409."""
    existing = _make_scope()
    with patch("app.services.scope_service.create_scope", return_value=existing):
        r = await client.post("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /scopes/{scope_id}  — Crossplane provider-http target
# ---------------------------------------------------------------------------

async def test_post_by_scope_id_creates_scope():
    """POST /scopes/{scope_id} is the actual URL Crossplane uses for create.
    It must behave identically to POST /scopes when path matches body.network."""
    created = _make_scope()
    with patch("app.services.scope_service.create_scope", return_value=created):
        r = await client.post("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 200
    assert r.json()["network"] == "10.20.30.0"


async def test_post_by_scope_id_mismatch_returns_400():
    """POST /scopes/{scope_id} must reject a body whose network != path scope_id.

    Without this check, Crossplane could POST to /scopes/10.20.30.0 with a body
    describing 10.20.40.0 and create the wrong scope silently.

    The body must be fully self-consistent for 10.20.40.0 so Pydantic subnet
    validation passes — then our path/body check fires and returns 400.
    """
    body = {
        "scopeName": "Different Scope",
        "network": "10.20.40.0",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.40.100",
        "endRange": "10.20.40.200",
        "leaseDurationDays": 8,
        "description": "",
        "gateway": "10.20.40.1",
        "dnsServers": ["10.0.0.53"],
        "dnsDomain": "lab.local",
        "exclusions": [],
        "failover": None,
    }
    r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 400
    err = _error(r.json())
    assert err["code"] == "SCOPE_ID_MISMATCH"
    assert "does not match" in err["message"]


async def test_post_by_scope_id_invalid_scope_id_returns_400():
    """POST /scopes/{scope_id} with malformed scope_id must return 400."""
    r = await client.post("/api/v1/scopes/10.20.999.0", json=_make_scope_dict())
    assert r.status_code == 400
    err = _error(r.json())
    assert err["code"] == "INVALID_SCOPE_ID"
    assert "10.20.999.0" in err["message"]


async def test_post_by_scope_id_with_hotstandby_failover():
    """POST /scopes/{scope_id} with HotStandby failover delegates to same service function."""
    failover_dict = {
        "partnerServer": "dhcp02.lab.local",
        "relationshipName": "rel1",
        "mode": "HotStandby",
        "serverRole": "Active",
        "reservePercent": 5,
        "loadBalancePercent": 0,
        "maxClientLeadTimeMinutes": 60,
    }
    body = _make_scope_dict(failover=failover_dict)
    created = _make_scope(failover=DhcpScopePayload(**body).failover)
    with patch("app.services.scope_service.create_scope", return_value=created) as mock_create:
        r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 200
    mock_create.assert_called_once()
    called_payload = mock_create.call_args[0][0]
    assert called_payload.failover is not None
    assert called_payload.failover.mode == "HotStandby"
    assert called_payload.failover.serverRole == "Active"


async def test_post_rejects_unknown_failover_fields():
    failover_dict = {
        "partnerServer": "dhcp02.lab.local",
        "relationshipName": "rel1",
        "mode": "HotStandby",
        "serverRole": "Active",
        "reservePercent": 5,
        "maxClientLeadTimeMinutes": 60,
        "unexpectedField": "unexpected-value",
    }
    body = _make_scope_dict(failover=failover_dict)
    r = await client.post("/api/v1/scopes/10.20.30.0", json=body)

    assert r.status_code == 422


async def test_post_by_scope_id_with_loadbalance_failover():
    """POST /scopes/{scope_id} with LoadBalance failover — serverRole must be normalised to Active."""
    failover_dict = {
        "partnerServer": "dhcp02.lab.local",
        "relationshipName": "rel1",
        "mode": "LoadBalance",
        "loadBalancePercent": 50,
        "maxClientLeadTimeMinutes": 60,
    }
    body = _make_scope_dict(failover=failover_dict)
    from app.models import DhcpFailover
    f = DhcpFailover(**failover_dict)
    created = _make_scope(failover=f)
    with patch("app.services.scope_service.create_scope", return_value=created) as mock_create:
        r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 200
    mock_create.assert_called_once()
    called_payload = mock_create.call_args[0][0]
    assert called_payload.failover is not None
    assert called_payload.failover.mode == "LoadBalance"
    assert called_payload.failover.serverRole == "Active"   # normalised
    assert called_payload.failover.reservePercent == 0      # normalised
    assert called_payload.failover.loadBalancePercent == 50


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------

async def test_put_update_scope():
    updated = _make_scope(scopeName="Updated Name")
    with patch("app.services.scope_service.update_scope", return_value=updated):
        r = await client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict(scopeName="Updated Name"))
    assert r.status_code == 200
    assert r.json()["scopeName"] == "Updated Name"


async def test_put_scope_not_found():
    from app.errors import ScopeNotFoundError
    with patch(
        "app.services.scope_service.update_scope",
        side_effect=ScopeNotFoundError("10.20.30.0"),
    ):
        r = await client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 404
    assert _error(r.json())["code"] == "SCOPE_NOT_FOUND"


async def test_put_scope_path_body_network_mismatch_returns_400():
    """PUT must reject requests where path scope_id != body network.

    Without this check, a Crossplane PUT to /scopes/10.20.30.0 with a body describing
    10.20.40.0 would silently apply the wrong scope's settings to the path scope,
    causing permanent reconciliation drift.
    """
    # Body must be self-consistent for 10.20.40.0 so Pydantic validation passes,
    # then our path/body check fires and rejects with 400.
    body = {
        "scopeName": "Different Scope",
        "network": "10.20.40.0",
        "subnetMask": "255.255.255.0",
        "startRange": "10.20.40.100",
        "endRange": "10.20.40.200",
        "leaseDurationDays": 8,
        "description": "",
        "gateway": "10.20.40.1",
        "dnsServers": ["10.0.0.53"],
        "dnsDomain": "lab.local",
        "exclusions": [],
        "failover": None,
    }
    r = await client.put("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 400
    err = _error(r.json())
    assert err["code"] == "SCOPE_ID_MISMATCH"
    assert "does not match" in err["message"]


async def test_put_scope_path_body_network_match_passes():
    """PUT succeeds when path scope_id matches body network field."""
    updated = _make_scope()
    with patch("app.services.scope_service.update_scope", return_value=updated):
        r = await client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict(network="10.20.30.0"))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

async def test_delete_scope():
    with patch("app.services.scope_service.delete_scope", return_value=None):
        r = await client.delete("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 204
    assert r.content == b""


async def test_delete_idempotent():
    """DELETE on non-existent scope must return 204."""
    with patch("app.services.scope_service.delete_scope", return_value=None):
        r = await client.delete("/api/v1/scopes/10.99.99.99")
    assert r.status_code == 204


async def test_delete_ps_error_during_assembly_returns_500():
    """Unexpected PowerShell failure during delete scope assembly must return 500, not 204.

    If _try_assemble_scope raises for a non-not-found reason (e.g. permission denied),
    delete_scope must propagate the error so Crossplane retries on the next cycle,
    rather than receiving a false 204 that causes it to remove the CR while the scope
    remains on the DHCP server.
    """
    with patch("app.services.scope_service.scope_exists", return_value=True), \
         patch("app.services.scope_service.assemble_scope_state",
               side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access denied", 5)):
        r = await client.delete("/api/v1/scopes/10.20.30.0")
    assert r.status_code == 500
    err = _error(r.json())
    assert err["code"] == "POWERSHELL_COMMAND_FAILED"
    assert "ps_error" not in err


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

async def test_powershell_error_500():
    with patch(
        "app.services.scope_service.create_scope",
        side_effect=PowerShellError("Add-DhcpServerv4Scope", "Access denied", 1),
    ):
        r = await client.post("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 500
    body = r.json()
    err = _error(body)
    assert err["code"] == "POWERSHELL_COMMAND_FAILED"
    assert err["message"] == "Failed to apply DHCP scope configuration"


async def test_powershell_timeout_returns_504():
    with patch(
        "app.services.scope_service.create_scope",
        side_effect=PowerShellError("Set-DhcpServerv4Scope", "PowerShell command timed out after 60 seconds", -1),
    ):
        r = await client.post("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 504
    assert _error(r.json())["code"] == "POWERSHELL_TIMEOUT"


async def test_powershell_already_exists_unhandled_returns_409():
    with patch(
        "app.services.scope_service.update_scope",
        side_effect=PowerShellError("Add-DhcpServerv4Failover", "relationship already exists", 1),
    ):
        r = await client.put("/api/v1/scopes/10.20.30.0", json=_make_scope_dict())
    assert r.status_code == 409
    assert _error(r.json())["code"] == "DHCP_CONFLICT"


async def test_failover_replication_failure_returns_500():
    from app.models import DhcpFailover

    failover = DhcpFailover(
        partnerServer="dhcp02.lab.local",
        relationshipName="rel1",
        mode="HotStandby",
        serverRole="Active",
        reservePercent=5,
        maxClientLeadTimeMinutes=60,
    )
    current = _make_scope(dnsServers=["10.0.0.53"], failover=failover)
    desired = _make_scope(dnsServers=["10.0.0.53", "10.0.0.54"], failover=failover)
    body = desired.model_dump(mode="json")

    with patch("app.services.scope_service.assemble_scope_state") as mock_assemble, \
         patch("app.services.scope_service.run_ps") as mock_ps:
        mock_assemble.side_effect = [current, desired]
        mock_ps.side_effect = [
            None,
            PowerShellError("Invoke-DhcpServerv4FailoverReplication", "replication failed", 1),
        ]
        r = await client.put("/api/v1/scopes/10.20.30.0", json=body)

    assert r.status_code == 500
    assert _error(r.json())["code"] == "POWERSHELL_COMMAND_FAILED"


async def test_unexpected_exception_returns_standard_500():
    with patch("app.services.scope_service.list_scopes", side_effect=TypeError("boom")):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 500
    err = _error(r.json())
    assert err["code"] == "INTERNAL_ERROR"
    assert err["message"] == "Internal server error"


async def test_method_not_allowed_uses_standard_error_shape():
    r = await client.post("/api/v1/scopes", json=_make_scope_dict())
    assert r.status_code == 405
    assert _error(r.json())["code"] == "METHOD_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_auth_required_when_token_set():
    import app.dependencies.auth as auth_mod
    original = auth_mod.settings.DHCP_API_TOKEN
    auth_mod.settings.DHCP_API_TOKEN = "secret-token"
    try:
        r = await client.get("/api/v1/scopes/10.20.30.0")
        assert r.status_code == 401
        err = _error(r.json())
        assert err["code"] == "UNAUTHORIZED"
        assert r.headers["www-authenticate"] == "Bearer"
    finally:
        auth_mod.settings.DHCP_API_TOKEN = original


async def test_auth_rejects_wrong_bearer_token():
    import app.dependencies.auth as auth_mod
    original = auth_mod.settings.DHCP_API_TOKEN
    auth_mod.settings.DHCP_API_TOKEN = "secret-token"
    try:
        r = await client.get(
            "/api/v1/scopes/10.20.30.0",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401
        assert _error(r.json())["code"] == "UNAUTHORIZED"
    finally:
        auth_mod.settings.DHCP_API_TOKEN = original


async def test_auth_disabled_when_token_empty():
    import app.dependencies.auth as auth_mod
    scope = _make_scope()
    original = auth_mod.settings.DHCP_API_TOKEN
    auth_mod.settings.DHCP_API_TOKEN = ""
    try:
        with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            r = await client.get("/api/v1/scopes/10.20.30.0")
        assert r.status_code == 200
    finally:
        auth_mod.settings.DHCP_API_TOKEN = original


async def test_auth_passes_with_correct_token():
    import app.dependencies.auth as auth_mod
    scope = _make_scope()
    original = auth_mod.settings.DHCP_API_TOKEN
    auth_mod.settings.DHCP_API_TOKEN = "secret-token"
    try:
        with patch("app.services.scope_service.assemble_scope_state", return_value=scope):
            r = await client.get(
                "/api/v1/scopes/10.20.30.0",
                headers={"Authorization": "Bearer secret-token"},
            )
        assert r.status_code == 200
    finally:
        auth_mod.settings.DHCP_API_TOKEN = original


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

async def test_healthz_endpoint():
    with patch("app.services.dhcp_service.validate_dhcp_environment"):
        r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Critical: GET/PUT roundtrip test
# ---------------------------------------------------------------------------

async def test_invalid_scope_id_returns_400():
    r = await client.get("/api/v1/scopes/10.20.999.0")
    assert r.status_code == 400
    assert _error(r.json())["code"] == "INVALID_SCOPE_ID"


async def test_invalid_scope_id_not_ip_returns_422():
    # Pattern validation on the path param catches non-IP strings before our validator
    r = await client.get("/api/v1/scopes/not-an-ip")
    assert r.status_code == 400
    assert _error(r.json())["code"] == "INVALID_SCOPE_ID"


async def test_invalid_request_body_returns_standard_validation_error():
    body = _make_scope_dict(startRange="not-an-ip")
    r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 422
    err = _error(r.json())
    assert err["code"] == "VALIDATION_ERROR"
    assert err["message"] == "Request validation failed"
    assert any(e["field"] == "body.startRange" for e in err["details"]["errors"])


async def test_post_empty_dns_servers_returns_422():
    body = _make_scope_dict(dnsServers=[])
    r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 422
    err = _error(r.json())
    assert err["code"] == "VALIDATION_ERROR"
    assert any(e["field"] == "body.dnsServers" for e in err["details"]["errors"])


async def test_put_empty_dns_servers_returns_422():
    body = _make_scope_dict(dnsServers=[])
    r = await client.put("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 422
    err = _error(r.json())
    assert err["code"] == "VALIDATION_ERROR"
    assert any(e["field"] == "body.dnsServers" for e in err["details"]["errors"])


async def test_invalid_subnet_relationship_returns_validation_error():
    body = _make_scope_dict(startRange="10.20.31.100", endRange="10.20.31.200")
    r = await client.post("/api/v1/scopes/10.20.30.0", json=body)
    assert r.status_code == 422
    err = _error(r.json())
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["errors"]


# ---------------------------------------------------------------------------
# GET /scopes — list all scopes
# ---------------------------------------------------------------------------

async def test_list_scopes_empty():
    """Empty DHCP server must return 200 with empty scopes and errors lists."""
    from app.models import DhcpScopeListResponse
    with patch("app.services.scope_service.list_scopes",
               return_value=DhcpScopeListResponse(scopes=[], errors=[])):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 200
    body = r.json()
    assert body == {"scopes": [], "errors": []}


async def test_list_scopes_single():
    """Single scope is returned in scopes list with the canonical shape."""
    from app.models import DhcpScopeListResponse
    scope = _make_scope()
    with patch("app.services.scope_service.list_scopes",
               return_value=DhcpScopeListResponse(scopes=[scope], errors=[])):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 200
    body = r.json()
    assert len(body["scopes"]) == 1
    assert body["errors"] == []
    # Verify all canonical top-level fields are present
    for field in ("scopeName", "network", "subnetMask", "startRange", "endRange",
                  "leaseDurationDays", "description", "gateway", "dnsServers",
                  "dnsDomain", "exclusions", "failover"):
        assert field in body["scopes"][0], f"canonical field '{field}' missing from list item"


async def test_list_scopes_multiple():
    """Multiple scopes are returned in scopes list."""
    from app.models import DhcpScopeListResponse
    scope_a = DhcpScopePayload(
        scopeName="Scope-A", network="10.20.30.0", subnetMask="255.255.255.0",
        startRange="10.20.30.100", endRange="10.20.30.200", leaseDurationDays=8,
        description="", gateway="10.20.30.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    scope_b = DhcpScopePayload(
        scopeName="Scope-B", network="10.20.31.0", subnetMask="255.255.255.0",
        startRange="10.20.31.100", endRange="10.20.31.200", leaseDurationDays=8,
        description="", gateway="10.20.31.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    with patch("app.services.scope_service.list_scopes",
               return_value=DhcpScopeListResponse(scopes=[scope_a, scope_b], errors=[])):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 200
    assert len(r.json()["scopes"]) == 2


async def test_list_scopes_partial_errors_returned():
    """Scopes with assembly errors appear in errors[], valid scopes still returned."""
    from app.models import DhcpScopeListError, DhcpScopeListResponse
    scope = _make_scope()
    err = DhcpScopeListError(scopeId="10.20.31.0", error="No DNS servers configured")
    with patch("app.services.scope_service.list_scopes",
               return_value=DhcpScopeListResponse(scopes=[scope], errors=[err])):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 200
    body = r.json()
    assert len(body["scopes"]) == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["scopeId"] == "10.20.31.0"
    assert "DNS" in body["errors"][0]["error"]


async def test_list_scopes_ps_error_returns_500():
    """PowerShell failure during list must return the standard safe 500 shape."""
    with patch(
        "app.services.scope_service.list_scopes",
        side_effect=PowerShellError("Get-DhcpServerv4Scope", "Access denied", 1),
    ):
        r = await client.get("/api/v1/scopes")
    assert r.status_code == 500
    body = r.json()
    assert _error(body)["code"] == "POWERSHELL_COMMAND_FAILED"


@pytest.mark.asyncio
async def test_list_scopes_sorted_numerically():
    """Scopes must be sorted numerically by network address regardless of PS return order."""
    from app.services.scope_service import list_scopes as svc_list_scopes

    scope_30 = DhcpScopePayload(
        scopeName="Scope-30", network="10.20.30.0", subnetMask="255.255.255.0",
        startRange="10.20.30.100", endRange="10.20.30.200", leaseDurationDays=8,
        description="", gateway="10.20.30.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    scope_9 = DhcpScopePayload(
        scopeName="Scope-9", network="10.20.9.0", subnetMask="255.255.255.0",
        startRange="10.20.9.100", endRange="10.20.9.200", leaseDurationDays=8,
        description="", gateway="10.20.9.1", dnsServers=["10.0.0.53"], dnsDomain="",
        exclusions=[], failover=None,
    )
    # "10.20.9.0" < "10.20.30.0" numerically but "10.20.30.0" < "10.20.9.0" lexicographically.
    # Correct numeric sort must put 10.20.9.0 first.
    # The all-scopes script emits one {scope, options, exclusions, failover} object per scope.
    raw_entries = [
        {"scope": {"ScopeId": "10.20.30.0"}, "options": [], "exclusions": [], "failover": None},
        {"scope": {"ScopeId": "10.20.9.0"},  "options": [], "exclusions": [], "failover": None},
    ]

    def fake_build(scope_id, state):
        return scope_30 if scope_id == "10.20.30.0" else scope_9

    with patch("app.services.scope_service.run_ps", return_value=raw_entries), \
         patch("app.services.scope_service.build_payload_from_scope_state", side_effect=fake_build):
        result = await svc_list_scopes()

    assert len(result.scopes) == 2
    assert str(result.scopes[0].network) == "10.20.9.0",  "10.20.9.0 must sort before 10.20.30.0 numerically"
    assert str(result.scopes[1].network) == "10.20.30.0"


async def test_list_scopes_item_shape_matches_single_scope_get(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    """Each item from GET /scopes must be byte-for-byte identical to GET /scopes/{scope_id}."""

    state = {
        "scope": mock_ps_scope_raw,
        "options": mock_ps_options_raw,
        "exclusions": mock_ps_exclusions_raw,
        "failover": None,
    }

    def fake_run_ps(cmd, parse_json=True, **_kwargs):
        if "ConvertTo-Json -Depth 10 -Compress" in cmd:
            return state
        # List call — no -ScopeId flag
        if "Get-DhcpServerv4Scope" in cmd and "-ScopeId" not in cmd:
            return [mock_ps_scope_raw]
        return None

    with patch("app.services.scope_service.run_ps", side_effect=fake_run_ps), \
         patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        list_r = await client.get("/api/v1/scopes")
        single_r = await client.get("/api/v1/scopes/10.20.30.0")

    assert list_r.status_code == 200
    assert single_r.status_code == 200
    list_body = list_r.json()
    single_item = single_r.json()
    assert len(list_body["scopes"]) == 1
    assert list_body["scopes"][0] == single_item, (
        f"GET /scopes item differs from GET /scopes/{{scope_id}}!\n"
        f"list[0]: {json.dumps(list_body['scopes'][0])}\n"
        f"single:  {json.dumps(single_item)}"
    )


async def test_list_scopes_failover_null_consistent(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    """failover: null in list items must match failover: null in single-scope GET."""

    state = {
        "scope": mock_ps_scope_raw,
        "options": mock_ps_options_raw,
        "exclusions": mock_ps_exclusions_raw,
        "failover": None,
    }

    def fake_run_ps(cmd, parse_json=True, **_kwargs):
        if "ConvertTo-Json -Depth 10 -Compress" in cmd:
            return state
        if "Get-DhcpServerv4Scope" in cmd and "-ScopeId" not in cmd:
            return [mock_ps_scope_raw]
        return None

    with patch("app.services.scope_service.run_ps", side_effect=fake_run_ps), \
         patch("app.services.ps_parsers.run_ps", side_effect=fake_run_ps):
        list_r = await client.get("/api/v1/scopes")

    assert list_r.status_code == 200
    item = list_r.json()["scopes"][0]
    assert "failover" in item
    assert item["failover"] is None


# ---------------------------------------------------------------------------
# Critical: GET/PUT roundtrip test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_put_roundtrip(
    mock_ps_scope_raw, mock_ps_options_raw, mock_ps_exclusions_raw
):
    """
    CRITICAL: The JSON that Crossplane sends as the PUT body must be byte-for-byte
    identical to the JSON returned by the GET endpoint.

    This test catches type mismatches (int vs str), key ordering differences,
    null handling, and array ordering issues.
    """
    from app.services.ps_parsers import assemble_scope_state as real_assemble

    # Build the "desired" payload — this is what Crossplane would send as PUT body
    put_payload = DhcpScopePayload(
        scopeName="Cluster-A Management",
        network="10.20.30.0",
        subnetMask="255.255.255.0",
        startRange="10.20.30.100",
        endRange="10.20.30.200",
        leaseDurationDays=8,
        description="Cluster A management network",
        gateway="10.20.30.1",
        dnsServers=["10.0.0.53", "10.0.0.54"],
        dnsDomain="lab.local",
        exclusions=[DhcpExclusion(startAddress="10.20.30.1", endAddress="10.20.30.99")],
        failover=None,
    )
    put_json = put_payload.model_dump(mode="json")

    # Simulate GET response assembled from the single PowerShell JSON object.
    state = {
        "scope": mock_ps_scope_raw,
        "options": mock_ps_options_raw,
        "exclusions": mock_ps_exclusions_raw,
        "failover": None,
    }

    with patch("app.services.ps_parsers.run_ps", return_value=state):
        get_payload = await real_assemble("10.20.30.0")
    get_json = get_payload.model_dump(mode="json")

    # Must be byte-for-byte identical — no sort_keys so field order matters too
    put_str = json.dumps(put_json, ensure_ascii=False)
    get_str = json.dumps(get_json, ensure_ascii=False)

    assert put_str == get_str, (
        f"GET/PUT mismatch!\nPUT: {put_str}\nGET: {get_str}"
    )
