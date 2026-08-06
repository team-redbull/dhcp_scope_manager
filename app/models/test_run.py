from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Suite = Literal["hermetic", "live", "all"]
RunStatus = Literal["running", "passed", "failed", "timeout", "error"]


class TestTarget(BaseModel):
    """The DHCP server one test run is pointed at.

    Deliberately part of the *request* rather than of the deployment's own
    configuration. A target baked into a values file is a target that can be
    misapplied to the wrong release; a target typed into each request cannot be.
    It also means the pod holds no test credentials at rest.
    """

    model_config = ConfigDict(extra="forbid")

    # Not a pytest test class. These names start with "Test" because they model
    # test *runs*, and pytest collects any imported class matching Test* — the
    # opt-out keeps a warning out of every module that imports them.
    __test__ = False

    dhcpServerHost: str = Field(
        min_length=1,
        description="Windows DHCP server the suite will create and delete a scope on",
        examples=["dhcp-test.lab.local"],
    )
    winrmUsername: str = Field(
        min_length=1,
        description="Account for the WinRM session. Needs DHCP Administrators on the target.",
    )
    # SecretStr so the value cannot leak through a repr, a validation error, or a
    # logged model dump — the three ways a password normally escapes.
    winrmPassword: SecretStr = Field(description="Never stored, never echoed back")
    winrmPort: int = Field(default=5986, ge=1, le=65535)
    winrmUseSsl: bool = True
    winrmAuth: Literal["kerberos", "ntlm", "credssp"] = "ntlm"
    winrmCertValidation: bool = True


class TestRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__ = False

    suite: Suite = Field(
        default="hermetic",
        description=(
            "hermetic = no DHCP server contact. live = tests/integration only. "
            "all = both."
        ),
    )
    target: TestTarget | None = Field(
        default=None,
        description="Required for 'live' and 'all'; rejected for 'hermetic'",
    )

    @model_validator(mode="after")
    def target_matches_suite(self) -> "TestRunRequest":
        needs_server = self.suite in ("live", "all")
        if needs_server and self.target is None:
            raise ValueError(
                f"suite '{self.suite}' contacts a real DHCP server, so 'target' is "
                f"required. There is deliberately no fallback to this deployment's "
                f"own DHCP settings."
            )
        if not needs_server and self.target is not None:
            # Not pedantry: accepting credentials for a suite that cannot use them
            # means a password crossed the wire for no reason.
            raise ValueError(
                "suite 'hermetic' contacts no DHCP server, so 'target' must be omitted"
            )
        return self


class TestRun(BaseModel):
    """A run's public shape. Carries no credentials — see TestTarget."""

    model_config = ConfigDict(extra="forbid")
    __test__ = False

    runId: str
    suite: Suite
    status: RunStatus
    startedAt: datetime
    finishedAt: datetime | None = None
    durationSeconds: float | None = None
    exitCode: int | None = None
    # Only the host, never the account or password: enough to answer "what did this
    # run touch?" without turning the registry into a credential store.
    targetHost: str | None = None
    output: str | None = None


class TestRunSummary(BaseModel):
    """List-endpoint shape — the same fields minus the captured output."""

    model_config = ConfigDict(extra="forbid")
    __test__ = False

    runId: str
    suite: Suite
    status: RunStatus
    startedAt: datetime
    finishedAt: datetime | None = None
    durationSeconds: float | None = None
    exitCode: int | None = None
    targetHost: str | None = None


class TestRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    __test__ = False

    runs: list[TestRunSummary]
