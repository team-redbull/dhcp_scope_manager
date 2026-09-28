from __future__ import annotations
from ipaddress import IPv4Address, IPv4Network
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.ip_utils import ip_to_int

from app.models.exclusion import DhcpExclusion
from app.models.failover import DhcpFailover

# The only mask for which a gateway or a distribution range can be derived. Any other
# mask requires both explicitly — see DhcpScopePayload.resolve_default_gateway and
# resolve_default_range.
DEFAULT_SUBNET_MASK = IPv4Address("255.255.255.0")

# Host octets of the derived distribution range: the whole /24 except the network
# address, the broadcast address, and .254.
#
# .254 is held back because it is the derived gateway (.255 is the broadcast address and
# validate_subnet_consistency refuses it outright). Ending at .254 would make the two
# derivations collide on every values file that omits both — the gateway would sit inside
# the distribution range uncovered by any exclusion, which
# gateway_not_in_distribution_range rejects. Stopping one short means the minimal file
# (a bare `network:`) resolves to a valid scope instead of a 422.
DERIVED_RANGE_FIRST_OCTET = 1
DERIVED_RANGE_LAST_OCTET = 253


class DhcpScopeBody(BaseModel):
    """The wire shape of a scope: state only, no identity.

    The scope address is the resource identifier and lives solely in the URL
    path, so it is deliberately absent here.  This model is both the POST/PUT
    request body and the single-scope GET/POST/PUT response, which is what
    makes GET/PUT parity (§9) hold by construction.
    """

    model_config = ConfigDict(extra="forbid")

    # Field ordering is CRITICAL — Crossplane compares GET response to PUT body byte-for-byte.
    # Do NOT reorder these fields.
    scopeName: str = Field(
        min_length=1,
        max_length=256,
        description="Human-readable display name for the scope",
        examples=["Cluster-A Management"],
    )

    @field_validator("scopeName")
    @classmethod
    def scope_name_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scopeName must not be blank or whitespace-only")
        return v
    subnetMask: IPv4Address = Field(
        default=DEFAULT_SUBNET_MASK,
        description="Subnet mask. Defaults to 255.255.255.0 when not supplied.",
        examples=["255.255.255.0"],
    )

    @field_validator("subnetMask", mode="before")
    @classmethod
    def normalize_subnet_mask(cls, v: object) -> object:
        """Normalize null/empty to the default mask.

        Unlike gateway, subnetMask has no 'unset' meaning — every DHCP scope has a
        mask — so omitted, null and "" all collapse to the same default rather than
        needing to stay distinguishable.
        """
        if v is None or v == "":
            return DEFAULT_SUBNET_MASK
        return v
    startRange: Optional[IPv4Address] = Field(
        default=None,
        description=(
            "First IP address in the DHCP distribution range. Omit it — together with "
            "endRange — to derive the subnet's .1 address; requires a /24."
        ),
        examples=["10.20.30.100"],
    )
    endRange: Optional[IPv4Address] = Field(
        default=None,
        description=(
            "Last IP address in the DHCP distribution range. Omit it — together with "
            "startRange — to derive the subnet's .253 address; requires a /24."
        ),
        examples=["10.20.30.200"],
    )

    @field_validator("startRange", "endRange", mode="before")
    @classmethod
    def normalize_range_bound(cls, v: object) -> object:
        """Normalize null/empty to None, the 'derive it' state.

        Unlike gateway, a range bound has no 'unset' meaning — every DHCP scope
        distributes some range — so omitted, null and "" all collapse to the same
        derivation rather than needing to stay distinguishable. That is subnetMask's
        treatment, not gateway's, and it is why this needs no model_fields_set dance
        in validate_scope_request.
        """
        if v == "":
            return None
        return v
    leaseDurationDays: int = Field(
        ge=1,
        le=3650,
        description="Lease duration in days (1–3650)",
        examples=[8],
    )
    description: str = Field(
        default="",
        max_length=1024,
        description="Optional scope description",
    )

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: object) -> object:
        """Normalize null/None to '' — prevents Crossplane drift when description is unset."""
        if v is None:
            return ""
        return v
    gateway: Optional[IPv4Address] = Field(
        default=None,
        description=(
            "Default gateway sent to clients (DHCP option 3). Omit the key to derive "
            "the subnet's .254 address; send null or \"\" for no gateway at all."
        ),
        examples=["10.20.30.1"],
    )

    @field_validator("gateway", mode="before")
    @classmethod
    def normalize_gateway(cls, v: object) -> object:
        """Normalize empty gateway values to null — DHCP option 3 is optional.

        Only applies when the caller actually sent the key. Omitting it entirely is a
        third, distinct state meaning "derive the default", tracked via
        model_fields_set and resolved in DhcpScopePayload.
        """
        if v == "":
            return None
        return v
    dnsServers: list[IPv4Address] = Field(
        default_factory=list,
        min_length=1,
        description="Ordered list of DNS server IPs sent to clients (DHCP option 6)",
        examples=[["10.0.0.53", "10.0.0.54"]],
    )
    dnsDomain: str = Field(
        default="",
        max_length=256,
        description="DNS domain suffix sent to clients (DHCP option 15)",
        examples=["lab.local"],
    )

    @field_validator("dnsDomain", mode="before")
    @classmethod
    def normalize_dns_domain(cls, v: object) -> object:
        """Normalize null/None to '' — consistent with description field behavior."""
        if v is None:
            return ""
        return v
    nextServer: str = Field(
        default="",
        max_length=255,
        description=(
            "PXE boot server host name or IP sent to clients (DHCP option 66). "
            "\"\" means the option is not set. Must be set together with bootFile."
        ),
        examples=["boot.lab.local"],
    )
    bootFile: str = Field(
        default="",
        max_length=255,
        description=(
            "PXE boot file path or URL sent to clients (DHCP option 67). "
            "\"\" means the option is not set. Must be set together with nextServer."
        ),
        examples=["snponly.efi"],
    )

    @field_validator("nextServer", "bootFile", mode="before")
    @classmethod
    def normalize_boot_option(cls, v: object) -> object:
        """Normalize null/None to '' — consistent with description/dnsDomain behavior."""
        if v is None:
            return ""
        return v

    @field_validator("nextServer", "bootFile")
    @classmethod
    def boot_option_is_a_single_token(cls, v: str) -> str:
        """Reject values PowerShell would accept but a PXE client cannot use.

        Deliberately permissive beyond that: bootFile legitimately carries '/', '\\',
        '.' and '://'. Only whitespace and control characters are excluded — both are
        illegal in an option 66/67 string and both usually mean a values-file typo.
        """
        if not v:
            return v
        if v != v.strip():
            raise ValueError("must not have leading or trailing whitespace")
        if any(c.isspace() for c in v):
            raise ValueError("must not contain whitespace")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("must not contain control characters")
        return v

    exclusions: list[DhcpExclusion] = Field(
        default_factory=list,
        description="IP ranges excluded from distribution, sorted by startAddress",
    )
    failover: Optional[DhcpFailover] = Field(
        default=None,
        description="Failover configuration. null = no failover configured.",
    )

    @model_validator(mode="after")
    def sort_exclusions(self) -> "DhcpScopeBody":
        """Normalize exclusion list to ascending IP order — ensures GET/PUT parity."""
        if len(self.exclusions) > 1:
            self.exclusions = sorted(
                self.exclusions,
                key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
            )
        return self

    @model_validator(mode="after")
    def no_duplicate_exclusions(self) -> "DhcpScopeBody":
        seen: set[tuple] = set()
        for i, excl in enumerate(self.exclusions):
            key = (excl.startAddress, excl.endAddress)
            if key in seen:
                raise ValueError(
                    f"exclusions[{i}] {excl.startAddress}-{excl.endAddress} is a duplicate"
                )
            seen.add(key)
        return self

    @model_validator(mode="after")
    def no_overlapping_exclusions(self) -> "DhcpScopeBody":
        if len(self.exclusions) < 2:
            return self
        sorted_excl = sorted(
            self.exclusions,
            key=lambda x: (ip_to_int(x.startAddress), ip_to_int(x.endAddress)),
        )
        for i in range(len(sorted_excl) - 1):
            a = sorted_excl[i]
            b = sorted_excl[i + 1]
            if ip_to_int(a.endAddress) >= ip_to_int(b.startAddress):
                raise ValueError(
                    f"exclusions overlap: {a.startAddress}-{a.endAddress} "
                    f"overlaps with {b.startAddress}-{b.endAddress}"
                )
        return self

    @model_validator(mode="after")
    def end_range_gte_start_range(self) -> "DhcpScopeBody":
        # Unresolved bounds are not comparable. Base-class validators run before the
        # ones on DhcpScopePayload, so this sees the range before it is derived —
        # unguarded, an omitted range would be a TypeError on None and so a 500
        # rather than the 422 a bad body deserves. Both-or-neither is enforced in
        # DhcpScopePayload.resolve_default_range, and a derived range is ordered by
        # construction, so nothing escapes by returning early here.
        if self.startRange is None or self.endRange is None:
            return self
        if int(self.endRange) < int(self.startRange):
            raise ValueError(
                f"endRange {self.endRange} must be >= startRange {self.startRange}"
            )
        return self

    @model_validator(mode="after")
    def pxe_options_set_together(self) -> "DhcpScopeBody":
        """DHCP options 66 and 67 are optional, but meaningless alone.

        A boot server with no boot file — or a boot file with no server — leaves a
        host half-configured and silently unbootable, so half a pair is rejected
        rather than written. Both empty is the ordinary no-PXE case and is valid.
        Mirrored in scripts/validate_dhcp_values.py so CI rejects it at PR time.
        """
        if bool(self.nextServer) != bool(self.bootFile):
            missing, present = (
                ("bootFile", "nextServer") if self.nextServer else ("nextServer", "bootFile")
            )
            raise ValueError(
                f"{missing} is required when {present} is set: DHCP options 66 and 67 "
                f"must be set together, or both left unset"
            )
        return self


class _ScopeIdentity(BaseModel):
    """Carries the scope address on its own so it can be ordered ahead of the body.

    Pydantic collects fields in reverse-MRO order, so listing this base *after*
    DhcpScopeBody in DhcpScopePayload puts `scope` first.  Enforced by
    test_scope_payload_field_ordering.
    """

    scope: IPv4Address = Field(
        description="Network address — the DHCP scope ID used in all PowerShell cmdlets",
        examples=["10.20.30.0"],
    )


class DhcpScopePayload(DhcpScopeBody, _ScopeIdentity):
    """A scope plus its identity — the internal domain model.

    Built by the request dependency from the path address and the request body,
    so subnet validation still sees every field.  Serialized directly only by
    GET /api/v1/scopes, where each item needs to say which scope it is.
    """

    # Declared first so it runs before the subnet/range guards below — those must see
    # the resolved gateway, not the placeholder None.
    @model_validator(mode="after")
    def resolve_default_gateway(self) -> "DhcpScopePayload":
        """Derive the gateway from the scope address when the caller omitted the key.

        Three distinct states, only the first of which defaults:
          omitted        -> derive <network>.254 (requires a /24 mask)
          null or ""     -> no DHCP option 3, honoured as written
          an IP address  -> used as written

        The .254 convention only holds for a /24, so any other mask without an explicit
        gateway is a mismatch rather than a guess. The check is on the mask's *value*,
        so an omitted mask (which defaults to /24) and an explicit 255.255.255.0 behave
        identically.
        """
        if "gateway" in self.model_fields_set:
            return self
        if self.subnetMask != DEFAULT_SUBNET_MASK:
            raise ValueError(
                f"gateway is required when subnetMask is {self.subnetMask}: the default "
                f"gateway is only derivable for {DEFAULT_SUBNET_MASK}. "
                f"Set gateway explicitly, or send null for no gateway."
            )
        # Mask the host octet rather than adding to the scope address: a scope that is
        # not a proper network address still yields the right .254, and the malformed
        # address itself is reported by validate_subnet_consistency below.
        self.gateway = IPv4Address((int(self.scope) & ~0xFF) | 254)
        return self

    # After resolve_default_gateway so a file omitting both reports the gateway problem
    # first (it is the more specific one), and before the subnet/range guards below so
    # they see resolved bounds rather than None.
    @model_validator(mode="after")
    def resolve_default_range(self) -> "DhcpScopePayload":
        """Derive the distribution range from the scope address when both bounds are absent.

        Two states, unlike gateway's three:
          both omitted (or null/"")  -> derive <network>.1 – <network>.253
          both addresses             -> used as written

        Deliberately fixed rather than fitted around the exclusions: exclusions already
        carve holes inside a Windows range, so `.1–.253` minus the exclusions *is* "every
        address that is not excluded". Making the bounds themselves move with the
        exclusion list would couple two derivations that have to stay identical here, in
        the CI validator and in the chart.

        .253 rather than .254 keeps the derived range clear of the derived gateway — see
        DERIVED_RANGE_LAST_OCTET.

        Both-or-neither, like the PXE pair: a half-specified range would leave the other
        edge silently at a subnet-wide default, which is the fail-open case this rule is
        shaped to avoid.
        """
        start_set = self.startRange is not None
        end_set = self.endRange is not None
        if start_set and end_set:
            return self
        if start_set or end_set:
            missing, present = (
                ("endRange", "startRange") if start_set else ("startRange", "endRange")
            )
            raise ValueError(
                f"{missing} is required when {present} is set: the distribution range "
                f"must be given as a pair, or left out entirely to derive "
                f".{DERIVED_RANGE_FIRST_OCTET}–.{DERIVED_RANGE_LAST_OCTET}"
            )
        if self.subnetMask != DEFAULT_SUBNET_MASK:
            raise ValueError(
                f"startRange and endRange are required when subnetMask is "
                f"{self.subnetMask}: the default range is only derivable for "
                f"{DEFAULT_SUBNET_MASK}. Set both explicitly."
            )
        # Mask the host octet rather than adding to the scope address, for the same
        # reason as the gateway: a malformed scope address still yields the right
        # octets, and the address itself is reported by validate_subnet_consistency.
        base = int(self.scope) & ~0xFF
        self.startRange = IPv4Address(base | DERIVED_RANGE_FIRST_OCTET)
        self.endRange = IPv4Address(base | DERIVED_RANGE_LAST_OCTET)
        return self

    @model_validator(mode="after")
    def validate_subnet_consistency(self) -> "DhcpScopePayload":
        """Validate that scope/subnetMask is a valid subnet and all IPs fall within it."""
        # strict=True: raises if network has host bits set, or mask is non-contiguous
        try:
            subnet = IPv4Network(f"{self.scope}/{self.subnetMask}", strict=True)
        except ValueError as exc:
            raise ValueError(
                f"scope {self.scope} with subnetMask {self.subnetMask} "
                f"is not a valid subnet: {exc}"
            ) from exc

        for field, ip in [
            ("startRange", self.startRange),
            ("endRange", self.endRange),
        ]:
            if ip not in subnet:
                raise ValueError(f"{field} {ip} is not within subnet {subnet}")
        if self.gateway is not None and self.gateway not in subnet:
            raise ValueError(f"gateway {self.gateway} is not within subnet {subnet}")

        for i, excl in enumerate(self.exclusions):
            for attr in ("startAddress", "endAddress"):
                ip = getattr(excl, attr)
                if ip not in subnet:
                    raise ValueError(
                        f"exclusions[{i}].{attr} {ip} is not within subnet {subnet}"
                    )

        # Reject network and broadcast addresses in dynamic-assignment and routing fields.
        # These addresses are reserved: assigning them to gateway/range causes DHCP failure.
        net_addr = subnet.network_address
        bcast_addr = subnet.broadcast_address
        for field, ip in [
            ("startRange", self.startRange),
            ("endRange", self.endRange),
        ]:
            if ip == net_addr:
                raise ValueError(
                    f"{field} {ip} must not be the network address {net_addr}"
                )
            if ip == bcast_addr:
                raise ValueError(
                    f"{field} {ip} must not be the broadcast address {bcast_addr}"
                )
        if self.gateway is not None:
            if self.gateway == net_addr:
                raise ValueError(
                    f"gateway {self.gateway} must not be the network address {net_addr}"
                )
            if self.gateway == bcast_addr:
                raise ValueError(
                    f"gateway {self.gateway} must not be the broadcast address {bcast_addr}"
                )

        return self

    # Declared here rather than on DhcpScopeBody purely for ordering: base-class
    # validators run first, and this must stay *after* validate_subnet_consistency
    # so an out-of-subnet range reports the subnet error rather than this one.
    @model_validator(mode="after")
    def gateway_not_in_distribution_range(self) -> "DhcpScopePayload":
        """Validate that gateway is not inside the DHCP distribution pool unless excluded.

        If gateway falls within [startRange, endRange] and is not covered by any
        exclusion, the DHCP server could lease the gateway IP to a client, causing
        a network outage for all hosts on that subnet.
        """
        if self.gateway is None:
            return self
        gw_int = int(self.gateway)
        if not (int(self.startRange) <= gw_int <= int(self.endRange)):
            return self
        # Gateway is inside the distribution range — verify it is excluded.
        for excl in self.exclusions:
            if int(excl.startAddress) <= gw_int <= int(excl.endAddress):
                return self  # covered by an exclusion — safe
        raise ValueError(
            f"gateway {self.gateway} is inside the DHCP distribution range "
            f"{self.startRange}-{self.endRange} but is not covered by any exclusion. "
            f"Add an exclusion for {self.gateway} or move the gateway outside the range."
        )

    def body(self) -> DhcpScopeBody:
        """Project down to the wire shape returned by single-scope endpoints."""
        return DhcpScopeBody.model_construct(
            **{name: getattr(self, name) for name in DhcpScopeBody.model_fields}
        )
