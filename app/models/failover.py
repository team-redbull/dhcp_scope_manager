from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DhcpFailover(BaseModel):
    model_config = ConfigDict(extra="forbid")

    partnerServer: str = Field(
        min_length=1,
        max_length=255,
        description="FQDN of the partner DHCP server",
        examples=["dhcp02.lab.local"],
    )

    @field_validator("partnerServer")
    @classmethod
    def partner_server_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("partnerServer must not be blank or whitespace-only")
        return v

    relationshipName: str = Field(
        min_length=1,
        max_length=64,
        description="Failover relationship name (unique per DHCP server pair)",
        examples=["mce1-failover"],
    )

    @field_validator("relationshipName")
    @classmethod
    def relationship_name_not_whitespace_only(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("relationshipName must not be blank or whitespace-only")
        return v
    mode: Literal["HotStandby", "LoadBalance"] = Field(
        description="Failover mode: HotStandby (active/standby) or LoadBalance"
    )
    serverRole: Optional[Literal["Active", "Standby"]] = Field(
        default=None,
        description=(
            "Role of THIS server. Required when mode is 'HotStandby'. "
            "Normalized to 'Active' for LoadBalance (not used in that mode)."
        ),
    )
    reservePercent: int = Field(
        default=0,
        ge=0,
        le=100,
        description=(
            "Percentage of addresses reserved for the standby server. "
            "Used only in HotStandby mode. Normalized to 0 for LoadBalance."
        ),
    )
    loadBalancePercent: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description=(
            "Percentage of load handled by THIS server. "
            "Required when mode is 'LoadBalance'. "
            "Normalized to 0 for HotStandby (not used in that mode)."
        ),
    )
    maxClientLeadTimeMinutes: int = Field(
        ge=1,
        le=1440,
        description="Max client lead time in minutes (1–1440, i.e. up to 24 hours)",
    )
    @model_validator(mode="after")
    def enforce_mode_fields(self) -> "DhcpFailover":
        """Normalize and validate mode-specific fields.

        HotStandby: serverRole is required; loadBalancePercent is not used → normalized to 0.
        LoadBalance: loadBalancePercent is required; serverRole and reservePercent are not
                     used → normalized to canonical values ('Active' and 0).

        Normalization (not rejection) is intentional: it prevents GET/PUT parity mismatches
        when Helm values.yaml includes unused-mode fields.
        """
        if self.mode == "HotStandby":
            if self.serverRole is None:
                raise ValueError("serverRole is required when mode is 'HotStandby'")
            self.loadBalancePercent = 0
        else:  # LoadBalance
            if self.loadBalancePercent is None:
                raise ValueError("loadBalancePercent is required when mode is 'LoadBalance'")
            self.serverRole = "Active"
            self.reservePercent = 0
        return self
