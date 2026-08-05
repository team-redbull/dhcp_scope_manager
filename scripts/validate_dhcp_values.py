#!/usr/bin/env python3
"""Validate DHCP values repository structure and content.

Walks sites/ and validates:
  - sites/configValues.yaml exists and is valid YAML
  - Each site directory has values.yaml and mces/
  - Each MCE directory has values.yaml and hostedClusters/
  - Each hosted-cluster YAML file parses correctly and is not empty
  - DHCP business rules applied to the merged inheritance chain

Inheritance chain per cluster (last file wins):
  sites/<site>/values.yaml
  → sites/<site>/mces/<mce>/values.yaml
  → sites/<site>/mces/<mce>/hostedClusters/<cluster>.yaml

Usage:
  python3 scripts/validate_dhcp_values.py
  python3 scripts/validate_dhcp_values.py --sites-dir sites
  python3 scripts/validate_dhcp_values.py --site telAviv
  python3 scripts/validate_dhcp_values.py --mce prep-mce-tlv-a
  python3 scripts/validate_dhcp_values.py --cluster prep-tlv-gpu
  python3 scripts/validate_dhcp_values.py --verbose
  python3 scripts/validate_dhcp_values.py --fail-fast

Exit codes:
  0  All validations passed
  1  One or more validation errors
  2  Script or I/O error (missing sites directory, unreadable file)

Dependencies: pydantic>=2.8, PyYAML>=6.0 (see scripts/requirements.txt)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field as dc_field
from ipaddress import AddressValueError, IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# ---------------------------------------------------------------------------
# Validation error data class
# ---------------------------------------------------------------------------

@dataclass
class ValError:
    path: Path
    message: str
    details: str | None = None

    def relative(self, base: Path) -> str:
        try:
            return str(self.path.relative_to(base))
        except ValueError:
            return str(self.path)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def load_yaml_file(path: Path) -> tuple[dict | None, str | None]:
    """Return (data, None) on success or (None, error_message) on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read file: {exc}"
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"YAML parse error: {exc}"
    return data, None


def validate_yaml_file(path: Path, allow_empty: bool = False) -> list[ValError]:
    """Check that path is valid YAML and (by default) not empty."""
    errors: list[ValError] = []
    data, err = load_yaml_file(path)
    if err is not None:
        errors.append(ValError(path, "Invalid YAML", err))
        return errors
    if not allow_empty and not data:
        errors.append(ValError(path, "File is empty or contains only null"))
    return errors


# ---------------------------------------------------------------------------
# YAML deep-merge (Helm -f semantics)
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if value is None:
            base[key] = None
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def merge_yaml_files(*paths: Path) -> dict:
    """Load and deep-merge YAML files in order (later files win)."""
    merged: dict = {}
    for p in paths:
        data, _ = load_yaml_file(p)
        if isinstance(data, dict):
            _deep_merge(merged, data)
    return merged


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def iter_sites(sites_dir: Path, site_filter: str | None = None):
    """Yield site directories under sites_dir."""
    for entry in sorted(sites_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if site_filter and entry.name != site_filter:
            continue
        yield entry


def iter_mces(site_path: Path, mce_filter: str | None = None):
    """Yield MCE directories under sites/<site>/mces/."""
    mces_dir = site_path / "mces"
    if not mces_dir.is_dir():
        return
    for entry in sorted(mces_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if mce_filter and entry.name != mce_filter:
            continue
        yield entry


def iter_hosted_clusters(mce_path: Path, cluster_filter: str | None = None):
    """Yield hosted-cluster YAML files under sites/<site>/mces/<mce>/hostedClusters/."""
    hc_dir = mce_path / "hostedClusters"
    if not hc_dir.is_dir():
        return
    for entry in sorted(hc_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix not in (".yaml", ".yml"):
            continue
        if cluster_filter:
            # Accept both stem ("prep-tlv-gpu") and full name ("prep-tlv-gpu.yaml")
            if entry.name != cluster_filter and entry.stem != cluster_filter:
                continue
        yield entry


# ---------------------------------------------------------------------------
# Structural validators
# ---------------------------------------------------------------------------

def validate_global_config(sites_dir: Path) -> list[ValError]:
    errors: list[ValError] = []
    if not sites_dir.exists():
        errors.append(ValError(sites_dir, "sites directory does not exist"))
        return errors
    if not sites_dir.is_dir():
        errors.append(ValError(sites_dir, "sites path is not a directory"))
        return errors
    config = sites_dir / "configValues.yaml"
    if not config.exists():
        errors.append(ValError(config, "Missing required global config file: configValues.yaml"))
    else:
        errors.extend(validate_yaml_file(config, allow_empty=True))
    return errors


def validate_site(site_path: Path) -> list[ValError]:
    errors: list[ValError] = []
    values = site_path / "values.yaml"
    if not values.exists():
        errors.append(ValError(values, "Missing required site values file: values.yaml"))
    else:
        errors.extend(validate_yaml_file(values, allow_empty=True))
    mces = site_path / "mces"
    if not mces.exists():
        errors.append(ValError(mces, "Missing required directory: mces/"))
    elif not mces.is_dir():
        errors.append(ValError(mces, "mces exists but is not a directory"))
    return errors


def validate_mce(mce_path: Path) -> list[ValError]:
    errors: list[ValError] = []
    values = mce_path / "values.yaml"
    if not values.exists():
        errors.append(ValError(values, "Missing required MCE values file: values.yaml"))
    else:
        errors.extend(validate_yaml_file(values, allow_empty=True))
    hc_dir = mce_path / "hostedClusters"
    if not hc_dir.exists():
        errors.append(ValError(hc_dir, "Missing required directory: hostedClusters/"))
    elif not hc_dir.is_dir():
        errors.append(ValError(hc_dir, "hostedClusters exists but is not a directory"))
    return errors


def validate_hosted_cluster(cluster_path: Path) -> list[ValError]:
    """Validate a single hosted-cluster YAML file: parse + not empty."""
    return validate_yaml_file(cluster_path, allow_empty=False)


# ---------------------------------------------------------------------------
# DHCP content validation (self-contained Pydantic models)
# ---------------------------------------------------------------------------

def _ip_to_int(ip: IPv4Address) -> int:
    return int(ip)


_K8S_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$")


def _is_valid_k8s_label(name: str) -> bool:
    return len(name) <= 63 and bool(_K8S_DNS_LABEL_RE.match(name))


# Mirrors DEFAULT_SUBNET_MASK in app/models/scope.py. This script deliberately
# re-implements the model rather than importing it so CI can run without the app
# installed — the two must be kept in step.
_DEFAULT_SUBNET_MASK = IPv4Address("255.255.255.0")


class _CiExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    startAddress: IPv4Address
    endAddress: IPv4Address

    @model_validator(mode="after")
    def end_gte_start(self) -> "_CiExclusion":
        if int(self.endAddress) < int(self.startAddress):
            raise ValueError(
                f"endAddress {self.endAddress} must be >= startAddress {self.startAddress}"
            )
        return self


class _CiFailover(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partnerServer: str = Field(min_length=1, max_length=255)
    relationshipName: str = Field(min_length=1, max_length=64)
    mode: Literal["HotStandby", "LoadBalance"]
    serverRole: Optional[Literal["Active", "Standby"]] = None
    reservePercent: int = Field(default=0, ge=0, le=100)
    loadBalancePercent: Optional[int] = Field(default=None, ge=0, le=100)
    maxClientLeadTimeMinutes: int = Field(ge=1, le=1440)

    @field_validator("partnerServer", "relationshipName")
    @classmethod
    def not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank or whitespace-only")
        return v

    @model_validator(mode="after")
    def enforce_mode_fields(self) -> "_CiFailover":
        if self.mode == "HotStandby":
            if self.serverRole is None:
                raise ValueError("serverRole is required when mode is 'HotStandby'")
            self.loadBalancePercent = 0
        else:
            if self.loadBalancePercent is None:
                raise ValueError("loadBalancePercent is required when mode is 'LoadBalance'")
            self.serverRole = "Active"
            self.reservePercent = 0
        return self


class _CiScopePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scopeName: str = Field(min_length=1, max_length=256)
    network: IPv4Address
    subnetMask: IPv4Address = _DEFAULT_SUBNET_MASK
    startRange: IPv4Address
    endRange: IPv4Address
    leaseDurationDays: int = Field(ge=1, le=3650)
    description: str = Field(default="", max_length=1024)
    gateway: Optional[IPv4Address] = None
    dnsServers: list[IPv4Address] = Field(default_factory=list, min_length=1)
    dnsDomain: str = Field(default="", max_length=256)
    nextServer: str = Field(default="", max_length=255)
    bootFile: str = Field(default="", max_length=255)
    exclusions: list[_CiExclusion] = Field(default_factory=list)
    failover: Optional[_CiFailover] = None

    @field_validator("scopeName")
    @classmethod
    def scope_name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("scopeName must not be blank or whitespace-only")
        return v

    @field_validator("description", mode="before")
    @classmethod
    def normalize_description(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("subnetMask", mode="before")
    @classmethod
    def normalize_subnet_mask(cls, v: object) -> object:
        return _DEFAULT_SUBNET_MASK if v is None or v == "" else v

    @field_validator("gateway", mode="before")
    @classmethod
    def normalize_gateway(cls, v: object) -> object:
        return None if v == "" else v

    @field_validator("dnsDomain", mode="before")
    @classmethod
    def normalize_dns_domain(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("nextServer", "bootFile", mode="before")
    @classmethod
    def normalize_boot_option(cls, v: object) -> object:
        return "" if v is None else v

    @field_validator("nextServer", "bootFile")
    @classmethod
    def boot_option_is_a_single_token(cls, v: str) -> str:
        if not v:
            return v
        if v != v.strip():
            raise ValueError("must not have leading or trailing whitespace")
        if any(c.isspace() for c in v):
            raise ValueError("must not contain whitespace")
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
            raise ValueError("must not contain control characters")
        return v

    # Mirrors DhcpScopeBody.pxe_options_set_together in app/models/scope.py — catches
    # a half-configured PXE pair at PR time instead of at rack time.
    @model_validator(mode="after")
    def pxe_options_set_together(self) -> "_CiScopePayload":
        if bool(self.nextServer) != bool(self.bootFile):
            missing, present = (
                ("pxe.bootfile", "pxe.server")
                if self.nextServer
                else ("pxe.server", "pxe.bootfile")
            )
            raise ValueError(
                f"{missing} is required when {present} is set: DHCP options 66 and 67 "
                f"must be set together, or both left unset"
            )
        return self

    @model_validator(mode="after")
    def no_duplicate_exclusions(self) -> "_CiScopePayload":
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
    def no_overlapping_exclusions(self) -> "_CiScopePayload":
        if len(self.exclusions) < 2:
            return self
        sorted_excl = sorted(
            self.exclusions,
            key=lambda x: (_ip_to_int(x.startAddress), _ip_to_int(x.endAddress)),
        )
        for i in range(len(sorted_excl) - 1):
            a, b = sorted_excl[i], sorted_excl[i + 1]
            if _ip_to_int(a.endAddress) >= _ip_to_int(b.startAddress):
                raise ValueError(
                    f"exclusions overlap: {a.startAddress}-{a.endAddress} "
                    f"overlaps with {b.startAddress}-{b.endAddress}"
                )
        return self

    @model_validator(mode="after")
    def end_range_gte_start_range(self) -> "_CiScopePayload":
        if int(self.endRange) < int(self.startRange):
            raise ValueError(
                f"endRange {self.endRange} must be >= startRange {self.startRange}"
            )
        return self

    # Must run before validate_subnet_consistency and gateway_not_in_distribution_range
    # so those see the resolved gateway. Mirrors
    # DhcpScopePayload.resolve_default_gateway in app/models/scope.py.
    @model_validator(mode="after")
    def resolve_default_gateway(self) -> "_CiScopePayload":
        if "gateway" in self.model_fields_set:
            return self
        if self.subnetMask != _DEFAULT_SUBNET_MASK:
            raise ValueError(
                f"gateway is required when subnetMask is {self.subnetMask}: the default "
                f"gateway is only derivable for {_DEFAULT_SUBNET_MASK}. "
                f'Set gateway explicitly, or set it to "" for no gateway.'
            )
        self.gateway = IPv4Address((int(self.network) & ~0xFF) | 254)
        return self

    @model_validator(mode="after")
    def validate_subnet_consistency(self) -> "_CiScopePayload":
        try:
            subnet = IPv4Network(f"{self.network}/{self.subnetMask}", strict=True)
        except ValueError as exc:
            raise ValueError(
                f"network {self.network} with subnetMask {self.subnetMask} "
                f"is not a valid subnet: {exc}"
            ) from exc
        for fname, ip in [("startRange", self.startRange), ("endRange", self.endRange)]:
            if ip not in subnet:
                raise ValueError(f"{fname} {ip} is not within subnet {subnet}")
        if self.gateway is not None and self.gateway not in subnet:
            raise ValueError(f"gateway {self.gateway} is not within subnet {subnet}")
        for i, excl in enumerate(self.exclusions):
            for attr in ("startAddress", "endAddress"):
                ip = getattr(excl, attr)
                if ip not in subnet:
                    raise ValueError(f"exclusions[{i}].{attr} {ip} is not within subnet {subnet}")
        net_addr = subnet.network_address
        bcast_addr = subnet.broadcast_address
        for fname, ip in [("startRange", self.startRange), ("endRange", self.endRange)]:
            if ip == net_addr:
                raise ValueError(f"{fname} {ip} must not be the network address")
            if ip == bcast_addr:
                raise ValueError(f"{fname} {ip} must not be the broadcast address")
        if self.gateway is not None:
            if self.gateway == net_addr:
                raise ValueError(f"gateway {self.gateway} must not be the network address")
            if self.gateway == bcast_addr:
                raise ValueError(f"gateway {self.gateway} must not be the broadcast address")
        return self

    @model_validator(mode="after")
    def gateway_not_in_distribution_range(self) -> "_CiScopePayload":
        if self.gateway is None:
            return self
        gw_int = int(self.gateway)
        if not (int(self.startRange) <= gw_int <= int(self.endRange)):
            return self
        for excl in self.exclusions:
            if int(excl.startAddress) <= gw_int <= int(excl.endAddress):
                return self
        raise ValueError(
            f"gateway {self.gateway} is inside the DHCP distribution range "
            f"{self.startRange}-{self.endRange} but is not covered by any exclusion"
        )


def _get_nested(data: dict, *keys: str, default: Any = None) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is None:
            return default
    return current


def _nested_present(data: dict, *keys: str) -> bool:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return current is not None


# Only dhcp_values lives in the values repo. dhcp_api and crossplane are chart-owned
# (helm/values.yaml) — a platform-wide constant, not per-cluster config — so the merge
# chain this script walks never contains them and must not demand them.
_REQUIRED_PATHS: list[tuple[str, ...]] = [
    ("dhcp_values", "scopeName"),
    ("dhcp_values", "network"),
    ("dhcp_values", "startRange"),
    ("dhcp_values", "endRange"),
    ("dhcp_values", "leaseDurationDays"),
    ("dhcp_values", "dns", "servers"),
]


def _validate_dhcp_content(cluster_path: Path, merged: dict) -> list[str]:
    """Run DHCP business validation on merged values. Returns list of error strings.

    A cluster opts into DHCP management by defining dhcp_values in its own file.
    Clusters that do not define dhcp_values are silently skipped — even if a parent
    values.yaml defines dhcp_values defaults for other clusters.
    """
    cluster_data, _ = load_yaml_file(cluster_path)
    if not isinstance(cluster_data, dict) or "dhcp_values" not in cluster_data:
        return []
    if "dhcp_values" not in merged:
        return []

    errors: list[str] = []

    # Required fields
    for keys in _REQUIRED_PATHS:
        if not _nested_present(merged, *keys):
            errors.append(f"required field missing: {'.'.join(keys)}")

    dv = merged.get("dhcp_values")
    if not isinstance(dv, dict):
        errors.append(f"dhcp_values must be a YAML mapping, got {type(dv).__name__}")
        return errors

    # Pydantic scope validation
    dns = dv.get("dns") or {}
    pxe = dv.get("pxe") or {}
    kwargs = {
        "scopeName":         dv.get("scopeName"),
        "network":           dv.get("network"),
        "startRange":        dv.get("startRange"),
        "endRange":          dv.get("endRange"),
        "leaseDurationDays": dv.get("leaseDurationDays"),
        "description":       dv.get("description") or "",
        "dnsServers":        dns.get("servers") or [],
        "dnsDomain":         dns.get("domain") or "",
        "nextServer":        pxe.get("server") or "",
        "bootFile":          pxe.get("bootfile") or "",
        "exclusions":        dv.get("exclusions") or [],
        "failover":          dv.get("failover"),
    }
    # Pass these two only when the values file actually wrote them. Setting them
    # unconditionally would make every file look like an explicit null, and the
    # derived defaults would never fire — see _CiScopePayload.resolve_default_gateway.
    for key in ("subnetMask", "gateway"):
        if key in dv:
            kwargs[key] = dv[key]
    try:
        _CiScopePayload(**kwargs)
    except ValidationError as exc:
        for error in exc.errors():
            loc = ".".join(str(p) for p in error["loc"])
            msg = error["msg"].removeprefix("Value error, ")
            path = f"dhcp_values.{loc}" if loc else "dhcp_values"
            errors.append(f"{path}: {msg}")

    # Exclusion order warning (becomes error in strict mode)
    exclusions = _get_nested(merged, "dhcp_values", "exclusions") or []
    if isinstance(exclusions, list) and len(exclusions) >= 2:
        ints: list[int] = []
        for excl in exclusions:
            try:
                ints.append(int(IPv4Address(str(excl.get("startAddress", "")))))
            except (AddressValueError, ValueError, AttributeError):
                break
        else:
            if ints != sorted(ints):
                errors.append(
                    "dhcp_values.exclusions: not in ascending IP order "
                    "(causes Crossplane reconciliation drift)"
                )

    return errors


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

_RESET  = "\033[0m"
_RED    = "\033[31m"
_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_CYAN   = "\033[36m"


def _c(text: str, *codes: str, use_color: bool = True) -> str:
    return ("".join(codes) + text + _RESET) if use_color else text


def _ok(label: str, use_color: bool) -> str:
    return _c("OK", _GREEN, _BOLD, use_color=use_color) + f" {label}"


def _fail(label: str, use_color: bool) -> str:
    return _c("FAIL", _RED, _BOLD, use_color=use_color) + f" {label}"


# ---------------------------------------------------------------------------
# Core validation runner
# ---------------------------------------------------------------------------

@dataclass
class ClusterResult:
    site: str
    mce: str
    cluster: str
    cluster_path: Path
    errors: list[ValError] = dc_field(default_factory=list)
    content_errors: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.content_errors

    def label(self) -> str:
        return f"{self.site}/{self.mce}/{self.cluster}"


def run_validation(args: argparse.Namespace) -> int:
    """Run full validation. Returns exit code (0=pass, 1=fail, 2=error)."""
    use_color = not args.no_color and sys.stdout.isatty()
    fail_fast: bool = args.fail_fast
    verbose: bool = args.verbose
    strict: bool = getattr(args, "strict", False)
    output_fmt: str = getattr(args, "format", "text")

    repo_root = Path(getattr(args, "repo_root", ".") or ".").resolve()
    sites_dir = (
        Path(args.sites_dir).resolve()
        if Path(args.sites_dir).is_absolute()
        else (repo_root / args.sites_dir).resolve()
    )

    site_filter:    str | None = getattr(args, "site", None)
    mce_filter:     str | None = getattr(args, "mce", None)
    cluster_filter: str | None = getattr(args, "cluster", None)

    all_errors: list[ValError] = []
    cluster_results: list[ClusterResult] = []

    # ── 1. Global structure ──────────────────────────────────────────────────
    global_errors = validate_global_config(sites_dir)
    if global_errors:
        all_errors.extend(global_errors)
        if output_fmt == "text":
            print(f"Validating sites directory: {_c(str(sites_dir), _BOLD, use_color=use_color)}\n")
            print(_c("Global config:", _BOLD, use_color=use_color))
            for e in global_errors:
                print(f"  {_fail(e.relative(repo_root), use_color)}")
                print(f"    {e.message}")
                if e.details and verbose:
                    print(f"    {_c(e.details, _DIM, use_color=use_color)}")
        if fail_fast or not sites_dir.is_dir():
            _print_summary(all_errors, cluster_results, output_fmt, use_color)
            return 1

    global_config_path = sites_dir / "configValues.yaml"

    if output_fmt == "text":
        print(f"Validating sites directory: {_c(str(sites_dir), _BOLD, use_color=use_color)}\n")
        if global_config_path.exists():
            print(f"Global config:\n  {_ok('configValues.yaml', use_color)}\n")

    # ── 2. Walk sites ────────────────────────────────────────────────────────
    for site_path in iter_sites(sites_dir, site_filter):
        site_errors = validate_site(site_path)
        if site_errors:
            all_errors.extend(site_errors)
            if output_fmt == "text":
                print(_c(f"Site: {site_path.name}", _BOLD, use_color=use_color))
                for e in site_errors:
                    print(f"  {_fail(e.relative(repo_root), use_color)}")
                    print(f"    {e.message}")
                    if e.details and verbose:
                        print(f"    {_c(e.details, _DIM, use_color=use_color)}")
            if fail_fast:
                _print_summary(all_errors, cluster_results, output_fmt, use_color)
                return 1
            continue

        site_values_path = site_path / "values.yaml"

        if output_fmt == "text":
            print(_c(f"Site: {site_path.name}", _BOLD, use_color=use_color))
            print(f"  {_ok('values.yaml', use_color)}")

        # ── 3. Walk MCEs ─────────────────────────────────────────────────────
        for mce_path in iter_mces(site_path, mce_filter):
            mce_errors = validate_mce(mce_path)
            if mce_errors:
                all_errors.extend(mce_errors)
                if output_fmt == "text":
                    print(f"  MCE: {_c(mce_path.name, _CYAN, use_color=use_color)}")
                    for e in mce_errors:
                        print(f"    {_fail(e.relative(repo_root), use_color)}")
                        print(f"      {e.message}")
                if fail_fast:
                    _print_summary(all_errors, cluster_results, output_fmt, use_color)
                    return 1
                continue

            mce_values_path = mce_path / "values.yaml"

            if output_fmt == "text":
                print(f"  MCE: {_c(mce_path.name, _CYAN, use_color=use_color)}")
                print(f"    {_ok('values.yaml', use_color)}")
                print(f"    HostedClusters:")

            # ── 4. Walk hosted clusters ───────────────────────────────────────
            for cluster_path in iter_hosted_clusters(mce_path, cluster_filter):
                result = ClusterResult(
                    site=site_path.name,
                    mce=mce_path.name,
                    cluster=cluster_path.stem,
                    cluster_path=cluster_path,
                )

                # Structural + YAML parse check
                struct_errors = validate_hosted_cluster(cluster_path)
                result.errors.extend(struct_errors)

                # DHCP content validation on merged chain
                dhcp_skipped = False
                if not struct_errors:
                    # configValues.yaml is the base of the merge, matching the
                    # valueFiles list Argo CD hands to Helm (hcAppset.yaml). Without
                    # it, a cluster that inherits leaseDurationDays or dns.servers
                    # from the global layer is reported as missing them.
                    chain = [p for p in (global_config_path, site_values_path,
                                         mce_values_path, cluster_path)
                             if p.exists()]
                    merged = merge_yaml_files(*chain)
                    cluster_data, _ = load_yaml_file(cluster_path)
                    dhcp_skipped = not (isinstance(cluster_data, dict) and "dhcp_values" in cluster_data)
                    content_errs = _validate_dhcp_content(cluster_path, merged)
                    if strict or True:  # always validate content when schema is present
                        result.content_errors.extend(content_errs)

                cluster_results.append(result)
                all_errors.extend(result.errors)

                if output_fmt == "text":
                    label = cluster_path.name
                    if result.ok:
                        if dhcp_skipped:
                            print(f"      {_ok(label, use_color)} {_c('(dhcp skipped)', _DIM, use_color=use_color)}")
                        else:
                            print(f"      {_ok(label, use_color)}")
                    else:
                        print(f"      {_fail(label, use_color)}")
                        for e in result.errors:
                            print(f"        {e.message}")
                            if e.details:
                                print(f"        {_c(e.details, _DIM, use_color=use_color)}")
                        for ce in result.content_errors:
                            print(f"        {_c('ERROR', _RED, _BOLD, use_color=use_color)} {ce}")

                    if verbose and result.ok and result.content_errors:
                        for ce in result.content_errors:
                            print(f"        {_c('ERROR', _RED, _BOLD, use_color=use_color)} {ce}")

                if fail_fast and not result.ok:
                    _print_summary(all_errors, cluster_results, output_fmt, use_color)
                    return 1

        if output_fmt == "text":
            print()

    # ── 5. Summary ───────────────────────────────────────────────────────────
    return _print_summary(all_errors, cluster_results, output_fmt, use_color)


def _print_summary(
    all_errors: list[ValError],
    cluster_results: list[ClusterResult],
    output_fmt: str,
    use_color: bool,
) -> int:
    has_errors = bool(all_errors) or any(not r.ok for r in cluster_results)

    if output_fmt == "json":
        output = {
            "passed": not has_errors,
            "global_errors": [
                {"path": str(e.path), "message": e.message, "details": e.details}
                for e in all_errors
                if not any(r.cluster_path == e.path for r in cluster_results)
            ],
            "clusters": [
                {
                    "label":          r.label(),
                    "site":           r.site,
                    "mce":            r.mce,
                    "cluster":        r.cluster,
                    "path":           str(r.cluster_path),
                    "passed":         r.ok,
                    "struct_errors":  [{"path": str(e.path), "message": e.message} for e in r.errors],
                    "content_errors": r.content_errors,
                }
                for r in cluster_results
            ],
        }
        print(json.dumps(output, indent=2))
        return 0 if not has_errors else 1

    # Text summary
    total = len(cluster_results)
    n_pass = sum(1 for r in cluster_results if r.ok)
    n_fail = total - n_pass

    print("═" * 60)
    print(f"Validated {total} hosted cluster(s):")
    print(f"  {_c(f'{n_pass} passed', _GREEN if n_pass else _DIM, _BOLD, use_color=use_color)}")
    print(f"  {_c(f'{n_fail} failed', _RED if n_fail else _DIM, _BOLD, use_color=use_color)}")

    if has_errors:
        failed = [r for r in cluster_results if not r.ok]
        if failed:
            print("\nFailed clusters:")
            for r in failed:
                print(f"  {_c('FAIL', _RED, _BOLD, use_color=use_color)}  {r.label()}")
        print()
        return 1
    print()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="validate_dhcp_values.py",
        description="Validate DHCP values repository structure and content.",
    )
    p.add_argument(
        "--repo-root", metavar="DIR", default=".",
        help="Repository root directory (default: current working directory)",
    )
    p.add_argument(
        "--sites-dir", metavar="DIR", default="sites",
        help="Path to sites/ directory, relative to --repo-root (default: sites)",
    )
    p.add_argument("--site",    metavar="NAME", help="Validate only this site")
    p.add_argument("--mce",     metavar="NAME", help="Validate only this MCE")
    p.add_argument(
        "--cluster", metavar="NAME",
        help="Validate only this cluster (stem or full filename, e.g. prep-tlv-gpu or prep-tlv-gpu.yaml)",
    )
    p.add_argument("--verbose",   action="store_true", help="Print extra detail")
    p.add_argument("--fail-fast", action="store_true", help="Stop after the first error")
    p.add_argument("--strict",    action="store_true", help="Enable stricter content checks")
    p.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI color output")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    sys.exit(run_validation(args))


if __name__ == "__main__":
    main()
