"""End-to-end tests against a real Windows DHCP server.

Skipped unless DHCP_IT=1 — see conftest.py and README.md in this directory.

Why this suite exists: the 700-odd unit tests all mock the PowerShell transport,
so they verify which cmdlet *strings* the service builds, never what Windows does
with them. Two bugs lived behind that seam — POST silently discarding half the
payload on an existing scope, and an under-specified PUT wiping PXE options and
exclusions — both of which these tests would have caught on the first run.

Every test owns TEST_SCOPE exclusively and deletes it on the way in and out, so
the suite is order-independent and safe to re-run after a failure.
"""
import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app

# Opt-in rather than auto-detect: these tests create and delete real scopes on
# whatever DHCP_SERVER_HOST points at, and inferring consent from the presence of
# a hostname would let a stray environment variable mutate a live server.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.getenv("DHCP_IT") != "1",
        reason="live DHCP integration tests — set DHCP_IT=1 (see tests/integration/README.md)",
    ),
]

# A scope address deliberately outside any range the test environment routes.
# Add-DhcpServerv4Scope only writes a row in dhcp.mdb — it allocates nothing and
# needs no matching network — so this cannot collide with real infrastructure.
TEST_SCOPE = "10.77.88.0"

BASE_BODY = {
    "scopeName": "it-lifecycle",
    "subnetMask": "255.255.255.0",
    "startRange": "10.77.88.100",
    "endRange": "10.77.88.200",
    "leaseDurationDays": 8,
    "description": "integration test scope",
    "gateway": "10.77.88.254",
    "dnsServers": ["10.0.0.53", "10.0.0.54"],
    "dnsDomain": "lab.local",
    "nextServer": "boot.lab.local",
    "bootFile": "snponly.efi",
    "exclusions": [{"startAddress": "10.77.88.1", "endAddress": "10.77.88.99"}],
    "failover": None,
}


def _body(**overrides):
    return {**BASE_BODY, **overrides}


class _Client:
    """Drives the real ASGI app: router, validation, service and PSRP all live."""

    async def request(self, method, path, **kwargs):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport, base_url="http://testserver", timeout=180.0
        ) as c:
            return await c.request(method, path, **kwargs)

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, **kw):
        return await self.request("POST", path, **kw)

    async def put(self, path, **kw):
        return await self.request("PUT", path, **kw)

    async def delete(self, path, **kw):
        return await self.request("DELETE", path, **kw)


client = _Client()


@pytest_asyncio.fixture(autouse=True)
async def clean_scope():
    """Delete TEST_SCOPE before and after each test.

    Deleting first matters as much as deleting after: a previous run that died
    between create and cleanup would otherwise hand the next run a pre-existing
    scope and quietly turn a create test into a convergence test.

    Must be @pytest_asyncio.fixture, not @pytest.fixture: the repo has no
    asyncio_mode setting, so pytest-asyncio runs strict, where a plain
    @pytest.fixture on an async generator is handed to the test un-awaited and
    the body silently never runs — leaving a stale scope behind.
    """
    await client.delete(f"/api/v1/scopes/{TEST_SCOPE}")
    yield
    await client.delete(f"/api/v1/scopes/{TEST_SCOPE}")


async def test_environment_is_configured():
    """Fail loudly rather than testing a mock by accident."""
    assert os.getenv("DHCP_TRANSPORT") == "psrp", (
        "integration tests require DHCP_TRANSPORT=psrp against a real server"
    )
    assert os.getenv("DHCP_SERVER_HOST"), "DHCP_SERVER_HOST must point at the DHCP server"


# ---------------------------------------------------------------------------
# GET / PUT parity — CLAUDE.md §12
# ---------------------------------------------------------------------------

async def test_get_matches_the_body_that_created_it():
    """GET must return exactly the body POST was given (§5, §9).

    This is the invariant Crossplane's containment check rests on: if GET and the
    rendered body can disagree on any field, reconciliation never converges.
    """
    created = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    assert created.status_code == 200, created.text

    fetched = await client.get(f"/api/v1/scopes/{TEST_SCOPE}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == BASE_BODY
    assert created.json() == fetched.json()


async def test_get_field_order_is_canonical():
    """Field order is part of the contract and is asserted on real server output."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    fetched = await client.get(f"/api/v1/scopes/{TEST_SCOPE}")
    assert list(fetched.json().keys()) == list(BASE_BODY.keys())


# ---------------------------------------------------------------------------
# POST — idempotency and convergence (CLAUDE.md §2)
# ---------------------------------------------------------------------------

async def test_post_on_existing_scope_converges_every_field():
    """Regression test for the reported bug.

    POST on an existing scope used to return 200 while silently discarding
    scopeName, startRange, endRange, leaseDurationDays and description — the five
    fields carried only by Add-DhcpServerv4Scope, which the already-exists path
    skipped. The response echoed the *old* state, so the caller could not tell.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())

    changed = _body(
        scopeName="it-converged",
        startRange="10.77.88.150",
        endRange="10.77.88.180",
        leaseDurationDays=3,
        description="converged by POST",
    )
    second = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=changed)
    assert second.status_code == 200, second.text

    # The response must describe what was asked for, not what was already there.
    assert second.json() == changed
    # And the server must actually hold it — not just echo the request back.
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json() == changed


async def test_post_is_idempotent_for_identical_state():
    """Re-POSTing the same body must stay 200 and change nothing (§2)."""
    first = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    second = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == BASE_BODY


async def test_post_converges_exclusion_removal_on_existing_scope():
    """Exclusion *removals* used to be impossible via POST — the create path only added."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    without = _body(exclusions=[])
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=without)
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["exclusions"] == []


async def test_post_converges_pxe_removal_on_existing_scope():
    """Options 66/67 need explicit Remove calls — Windows keeps a value that stopped being written."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.post(
        f"/api/v1/scopes/{TEST_SCOPE}", json=_body(nextServer="", bootFile="")
    )
    assert resp.status_code == 200, resp.text
    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["nextServer"] == ""
    assert fetched["bootFile"] == ""


# ---------------------------------------------------------------------------
# PUT — diff-based convergence (CLAUDE.md §6)
# ---------------------------------------------------------------------------

async def test_put_converges_scope_parameters():
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    desired = _body(scopeName="it-renamed", leaseDurationDays=21, description="updated")
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=desired)
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json() == desired


@pytest.mark.parametrize(
    "start,end,label",
    [
        ("10.77.88.50", "10.77.88.250", "widening"),
        ("10.77.88.120", "10.77.88.180", "narrowing"),
        ("10.77.88.150", "10.77.88.250", "mixed — routes through the union"),
        ("10.77.88.10", "10.77.88.40", "disjoint — routes through the union"),
    ],
)
async def test_put_range_transitions(start, end, label):
    """Windows accepts a new range only as a superset or subset of the current one.

    _range_transition_steps widens to the union first for the mixed and disjoint
    cases. Only a real server proves that actually satisfies DHCP 20023.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    # Keep the gateway clear of the pool; .254 is inside the disjoint case's union.
    desired = _body(startRange=start, endRange=end, gateway=None, exclusions=[])
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=desired)
    assert resp.status_code == 200, f"{label}: {resp.text}"
    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["startRange"] == start
    assert fetched["endRange"] == end


# The tests above cover clearing an option; these cover *changing* one. They are
# different code paths — the diff decides whether to issue a cmdlet at all, and a
# diff that wrongly concludes "unchanged" issues nothing and leaves the server
# stale while still returning 200. That is the exact shape of the POST bug, and a
# reset-to-default test cannot see it.

async def test_put_changes_gateway_to_a_different_address():
    """Option 3 rewritten, not removed."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    desired = _body(gateway="10.77.88.253")
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=desired)
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["gateway"] == "10.77.88.253"


async def test_put_clears_gateway_with_explicit_null():
    """Option 3 *removed*, which needs Remove-DhcpServerv4OptionValue.

    Windows keeps an option value that merely stopped being written, so a code
    path that only ever calls Set- leaves the old router in place while the API
    reports success. Only a real server shows the difference.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=_body(gateway=None))
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["gateway"] is None


async def test_put_reorders_dns_servers():
    """Order is primary/secondary, never sorted.

    Crossplane compares lists by order, so if the API normalised or sorted these
    the GET would stop matching the rendered body and PUT every 60s forever.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    reversed_servers = list(reversed(BASE_BODY["dnsServers"]))
    resp = await client.put(
        f"/api/v1/scopes/{TEST_SCOPE}", json=_body(dnsServers=reversed_servers)
    )
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["dnsServers"] == reversed_servers


async def test_put_changes_dns_domain():
    """Option 15 rewritten."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=_body(dnsDomain="other.local"))
    assert resp.status_code == 200, resp.text
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["dnsDomain"] == "other.local"


async def test_put_replaces_exclusions_with_a_different_set():
    """One PUT that both adds and removes — the set difference, not a reset.

    The base body excludes 10.77.88.1-99 as a single range. Replacing it with two
    narrower ranges means the diff has to issue a Remove for the old one and two
    Adds, and the result must be exactly the new set — not the union, which is
    what an add-only path would leave.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    replacement = [
        {"startAddress": "10.77.88.1", "endAddress": "10.77.88.10"},
        {"startAddress": "10.77.88.11", "endAddress": "10.77.88.20"},
    ]
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=_body(exclusions=replacement))
    assert resp.status_code == 200, resp.text
    # Sorted ascending by IP, and carrying exactly the new set.
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["exclusions"] == replacement


async def test_put_sets_pxe_options_that_were_previously_unset():
    """Options 66/67 written by PUT onto a scope created without them."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body(nextServer="", bootFile=""))
    resp = await client.put(
        f"/api/v1/scopes/{TEST_SCOPE}",
        json=_body(nextServer="pxe.lab.local", bootFile="undionly.kpxe"),
    )
    assert resp.status_code == 200, resp.text
    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["nextServer"] == "pxe.lab.local"
    assert fetched["bootFile"] == "undionly.kpxe"


async def test_put_changes_pxe_options_to_different_values():
    """Options 66/67 rewritten — one -OptionId per call, so both must land."""
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.put(
        f"/api/v1/scopes/{TEST_SCOPE}",
        json=_body(nextServer="tftp.lab.local", bootFile="ipxe.efi"),
    )
    assert resp.status_code == 200, resp.text
    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["nextServer"] == "tftp.lab.local"
    assert fetched["bootFile"] == "ipxe.efi"


async def test_put_rejects_subnet_mask_change_with_409():
    """Windows cannot change a mask in place, so a PUT that tries is refused (§6).

    /23 rather than something shorter: the new mask has to leave 10.77.88.0 a valid
    network address and keep every range, gateway and exclusion inside the subnet.
    A mask like 255.255.0.0 would make 10.77.88.0/16 host-bits-set and return 422
    from validation, never reaching the immutability guard this test is about.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.put(
        f"/api/v1/scopes/{TEST_SCOPE}", json=_body(subnetMask="255.255.254.0")
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "IMMUTABLE_FIELD"
    # The refusal must be total — the scope keeps its original mask.
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()["subnetMask"] == "255.255.255.0"


async def test_put_requires_the_mandatory_fields():
    """PUT is full-replace, so the required fields cannot be omitted.

    startRange/endRange are deliberately absent from this set: they derive
    (.1–.253), so a body without them is complete rather than partial.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json={"scopeName": "partial"})
    assert resp.status_code == 422
    missing = {e["field"].removeprefix("body.") for e in resp.json()["error"]["details"]["errors"]}
    assert {"leaseDurationDays", "dnsServers"} <= missing
    assert "startRange" not in missing
    assert "endRange" not in missing


async def test_put_omitting_optional_fields_resets_them():
    """PUT is full replacement: an omitted optional field is reset, not preserved.

    This pins *known and intended* behaviour rather than endorsing it. Crossplane
    always sends the fully rendered body from Git, so this path is only reachable
    by hand — but it is reachable, and it silently drops PXE config and exclusions,
    so it must not change without someone noticing.

    Keeping full-replace is what makes Git authoritative: if omission meant "leave
    alone", deleting a line from a values file could never remove anything.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())

    minimal = {
        "scopeName": "it-lifecycle",
        "startRange": "10.77.88.100",
        "endRange": "10.77.88.200",
        "leaseDurationDays": 8,
        "dnsServers": ["10.0.0.53", "10.0.0.54"],
    }
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=minimal)
    assert resp.status_code == 200, resp.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["dnsDomain"] == ""
    assert fetched["nextServer"] == ""
    assert fetched["bootFile"] == ""
    assert fetched["exclusions"] == []
    assert fetched["description"] == ""
    # gateway was omitted entirely, so it derives to the subnet's .254 rather than clearing.
    assert fetched["gateway"] == "10.77.88.254"


async def test_put_on_missing_scope_returns_404():
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE (CLAUDE.md §7) and the reconciliation loop (§9)
# ---------------------------------------------------------------------------

async def test_delete_is_idempotent():
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    first = await client.delete(f"/api/v1/scopes/{TEST_SCOPE}")
    second = await client.delete(f"/api/v1/scopes/{TEST_SCOPE}")
    assert first.status_code == second.status_code == 204
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).status_code == 404


async def test_full_reconciliation_cycle():
    """The exact sequence Crossplane drives: 404 → POST → GET → PUT → DELETE → 404."""
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).status_code == 404

    assert (await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())).status_code == 200
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json() == BASE_BODY

    desired = _body(scopeName="it-reconciled", leaseDurationDays=14)
    assert (await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=desired)).status_code == 200
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json() == desired

    assert (await client.delete(f"/api/v1/scopes/{TEST_SCOPE}")).status_code == 204
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).status_code == 404


async def test_scope_appears_in_the_list_endpoint():
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())
    listed = await client.get("/api/v1/scopes")
    assert listed.status_code == 200, listed.text
    payload = listed.json()

    entry = next((s for s in payload["scopes"] if s["scope"] == TEST_SCOPE), None)
    assert entry is not None, f"{TEST_SCOPE} missing from list"
    # A list item is the body shape plus a leading `scope` field (§5).
    assert list(entry.keys()) == ["scope"] + list(BASE_BODY.keys())
    assert {k: v for k, v in entry.items() if k != "scope"} == BASE_BODY

    addresses = [s["scope"] for s in payload["scopes"]]
    assert addresses == sorted(addresses, key=lambda a: tuple(int(o) for o in a.split("."))), (
        "list must be sorted ascending by scope address"
    )


async def test_create_scope_with_a_non_24_mask():
    """The other branch of the derived-default rule.

    /23 rather than something shorter so 10.77.88.0 stays a valid network address
    with every range and exclusion inside it. A non-/24 mask requires an explicit
    gateway — the API refuses to guess one — so this also pins that requirement
    against a real scope rather than against the model alone.
    """
    body = _body(
        subnetMask="255.255.254.0",
        gateway="10.77.88.254",
        exclusions=[{"startAddress": "10.77.88.1", "endAddress": "10.77.88.99"}],
    )
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert resp.status_code == 200, resp.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["subnetMask"] == "255.255.254.0"
    assert fetched == body


async def test_post_omitting_the_range_creates_the_derived_pool():
    """A body with no range must create a real scope running .1–.253.

    The unit suite can only prove the cmdlet string carries those bounds. Only a
    live Add-DhcpServerv4Scope proves Windows accepts a range that starts at the
    first host address, and that GET reads it back identically — which is what
    Crossplane's containment check compares against.
    """
    body = _body(exclusions=[], gateway=None)
    del body["startRange"]
    del body["endRange"]

    created = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert created.status_code == 200, created.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["startRange"] == "10.77.88.1"
    assert fetched["endRange"] == "10.77.88.253"
    assert created.json() == fetched


async def test_post_omitting_both_range_and_gateway_is_a_valid_scope():
    """The two derivations must not collide on the minimal body.

    .253 exists precisely so the derived gateway (.254) lands outside the derived
    pool. Against a real server this is the whole day-1 shape end to end: identity,
    lease and DNS, everything else resolved.
    """
    body = {
        "scopeName": "it-derived",
        "leaseDurationDays": 8,
        "dnsServers": ["10.0.0.53", "10.0.0.54"],
    }
    created = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert created.status_code == 200, created.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["startRange"] == "10.77.88.1"
    assert fetched["endRange"] == "10.77.88.253"
    assert fetched["gateway"] == "10.77.88.254"


async def test_put_removing_an_explicit_range_widens_to_the_derived_pool():
    """Deleting the keys is a live range change, not a no-op (§6 full replacement).

    .100–.200 to .1–.253 is a widening, so _range_transition_steps collapses it to a
    single Set-DhcpServerv4Scope — but that only holds if Windows accepts it, which
    is what makes this a live test rather than a diff test. This is also the exact
    edit a values file gets when someone drops the two lines.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())

    desired = _body(exclusions=[], gateway=None)
    del desired["startRange"]
    del desired["endRange"]
    resp = await client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=desired)
    assert resp.status_code == 200, resp.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["startRange"] == "10.77.88.1"
    assert fetched["endRange"] == "10.77.88.253"


async def test_put_narrowing_from_the_derived_pool_to_an_explicit_range():
    """And back again — adding the lines has to converge as reliably as removing them."""
    derived = _body(exclusions=[], gateway=None)
    del derived["startRange"]
    del derived["endRange"]
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=derived)

    resp = await client.put(
        f"/api/v1/scopes/{TEST_SCOPE}",
        json=_body(startRange="10.77.88.100", endRange="10.77.88.200"),
    )
    assert resp.status_code == 200, resp.text

    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched["startRange"] == "10.77.88.100"
    assert fetched["endRange"] == "10.77.88.200"


async def test_derived_range_scope_is_idempotent_on_repost():
    """A POST of a range-less body against the scope it already created writes nothing new.

    Idempotency is a property of the diff (§2), and the diff compares the *derived*
    bounds against what the server holds — so this only stays 200-with-no-drift if
    the derivation is stable across calls.
    """
    body = _body(exclusions=[], gateway=None)
    del body["startRange"]
    del body["endRange"]

    first = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    second = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert first.status_code == second.status_code == 200, second.text
    assert first.json() == second.json()
    assert (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json() == second.json()


async def test_concurrent_puts_on_one_scope_serialize():
    """ScopeLockManager against a real server.

    Windows DHCP does not serialise concurrent writes to one scope for us. Two
    PUTs racing on the same scope must not interleave into a state that is
    neither desired state — the per-scope lock is what prevents it, and only a
    real server exercises the actual cmdlet timings.
    """
    await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body())

    first = _body(scopeName="it-racer-a", leaseDurationDays=4, description="a")
    second = _body(scopeName="it-racer-b", leaseDurationDays=9, description="b")

    responses = await asyncio.gather(
        client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=first),
        client.put(f"/api/v1/scopes/{TEST_SCOPE}", json=second),
    )
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]

    # Whichever won, the scope must hold that one *entirely* — not one field from
    # each, which is what an unserialised pair of writes would leave behind.
    fetched = (await client.get(f"/api/v1/scopes/{TEST_SCOPE}")).json()
    assert fetched in (first, second), f"interleaved state: {fetched}"


# ---------------------------------------------------------------------------
# Validation at the boundary (CLAUDE.md §10)
# ---------------------------------------------------------------------------

async def test_body_carrying_the_scope_address_is_rejected():
    """Identity lives in the URL only — a body that repeats it is a 422, not a merge."""
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=_body(scope=TEST_SCOPE))
    assert resp.status_code == 422


async def test_half_a_pxe_pair_is_rejected():
    """nextServer without bootFile leaves a host silently unbootable (§2)."""
    resp = await client.post(
        f"/api/v1/scopes/{TEST_SCOPE}", json=_body(nextServer="boot.lab.local", bootFile="")
    )
    assert resp.status_code == 422


async def test_half_a_range_is_rejected():
    """One bound alone is a 422 — the pair is never half-derived."""
    body = _body()
    del body["endRange"]
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert resp.status_code == 422
    assert "endRange is required when startRange is set" in resp.text


async def test_non_24_mask_without_a_range_is_rejected():
    """The .1–.253 convention holds for a /24 only; anything else must be explicit."""
    body = _body(subnetMask="255.255.254.0", gateway="10.77.88.254")
    del body["startRange"]
    del body["endRange"]
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert resp.status_code == 422
    assert "startRange and endRange are required" in resp.text


async def test_derived_range_with_an_unexcluded_low_gateway_is_rejected():
    """Fail-closed: widening the pool over a router address is refused, not written.

    The one genuinely new hazard the derived range introduces — a gateway at .1 that
    was safely below an explicit startRange is inside the derived pool.
    """
    body = _body(gateway="10.77.88.1", exclusions=[])
    del body["startRange"]
    del body["endRange"]
    resp = await client.post(f"/api/v1/scopes/{TEST_SCOPE}", json=body)
    assert resp.status_code == 422
    assert "not covered by any exclusion" in resp.text
