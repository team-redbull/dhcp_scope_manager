from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    DHCP_API_TOKEN: str = ""  # empty = auth disabled
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    LOG_LEVEL: str = "INFO"
    POWERSHELL_COMMAND_TIMEOUT_SECONDS: int = Field(default=60, ge=1)
    POWERSHELL_ENV_CHECK_TIMEOUT_SECONDS: int = Field(default=15, ge=1)
    POWERSHELL_MAX_CONCURRENCY: int = Field(default=10, ge=1)

    # ------------------------------------------------------------------
    # Execution transport
    # ------------------------------------------------------------------
    # "local" — spawn powershell.exe on this host. Requires the API to run on
    #           a Windows box with the DHCP Server role (the test-env layout).
    # "psrp"  — run cmdlets on a remote Windows DHCP server over WinRM/PSRP.
    #           Lets the API run on Linux; no PowerShell executes locally.
    DHCP_TRANSPORT: Literal["local", "psrp"] = "local"

    # Target DHCP server for the psrp transport.
    #
    # Deliberately a single explicit host rather than a DNS alias or load
    # balancer. Under failover either peer can answer a read, and replication
    # lag or drift on the partner would make GET differ from the desired PUT
    # body — Crossplane would then PUT corrections in a loop, violating the
    # byte-identical GET/PUT requirement in CLAUDE.md section 9.
    DHCP_SERVER_HOST: str = ""
    WINRM_PORT: int = Field(default=5986, ge=1, le=65535)
    WINRM_USE_SSL: bool = True
    WINRM_AUTH: Literal["kerberos", "ntlm"] = "kerberos"
    WINRM_USERNAME: str = ""
    WINRM_PASSWORD: str = ""
    WINRM_CERT_VALIDATION: bool = True
    WINRM_CONNECTION_TIMEOUT_SECONDS: int = Field(default=30, ge=1)

    @property
    def is_psrp(self) -> bool:
        return self.DHCP_TRANSPORT == "psrp"

    @model_validator(mode="after")
    def _validate_transport(self) -> "Settings":
        """Fail fast at import time on an incoherent transport configuration.

        A misconfigured target is not recoverable at runtime, and surfacing it
        as a startup error is far clearer than a per-request 503.
        """
        if not self.is_psrp:
            return self

        if not self.DHCP_SERVER_HOST:
            raise ValueError(
                "DHCP_SERVER_HOST is required when DHCP_TRANSPORT='psrp' — "
                "set it to the DHCP server this API manages."
            )

        if self.WINRM_AUTH == "ntlm" and not (self.WINRM_USERNAME and self.WINRM_PASSWORD):
            raise ValueError(
                "WINRM_USERNAME and WINRM_PASSWORD are required when "
                "WINRM_AUTH='ntlm'. Prefer WINRM_AUTH='kerberos', which "
                "authenticates from a keytab or credential cache with no "
                "stored password."
            )

        return self


settings = Settings()
