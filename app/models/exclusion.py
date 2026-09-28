from __future__ import annotations
from ipaddress import IPv4Address
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DhcpExclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    startAddress: IPv4Address = Field(
        description="First IP address in the exclusion range",
        examples=["10.20.30.1"],
    )
    endAddress: IPv4Address = Field(
        description="Last IP address in the exclusion range",
        examples=["10.20.30.99"],
    )

    @model_validator(mode="after")
    def end_gte_start(self) -> "DhcpExclusion":
        if int(self.endAddress) < int(self.startAddress):
            raise ValueError(
                f"endAddress {self.endAddress} must be >= startAddress {self.startAddress}"
            )
        return self
