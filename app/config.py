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

    # "kerberos" — no stored password; authenticates from a keytab or ccache.
    # "ntlm"     — username/password, no delegation.
    # "credssp"  — username/password, and the credential is *delegated* to the
    #              DHCP server so it can authenticate onward to its failover
    #              partner. Required for the failover paths in CLAUDE.md §6/§7:
    #              Add-DhcpServerv4Failover, Add-DhcpServerv4FailoverScope and
    #              Invoke-DhcpServerv4FailoverReplication all act on the partner
    #              as the calling user, and a plain ntlm/kerberos WinRM session
    #              holds no credential to present there (the "double hop").
    #              Verified empirically: identical cmdlets fail under a network
    #              logon and succeed under one carrying a credential.
    #              The tradeoff is real — the password becomes recoverable on
    #              the DHCP server — so use a dedicated account holding only
    #              DHCP Administrators, never a Domain Admin. Note that accounts
    #              in the Protected Users group cannot delegate at all.
    WINRM_AUTH: Literal["kerberos", "ntlm", "credssp"] = "kerberos"
    WINRM_USERNAME: str = ""
    WINRM_PASSWORD: str = ""
    WINRM_CERT_VALIDATION: bool = True
    WINRM_CONNECTION_TIMEOUT_SECONDS: int = Field(default=30, ge=1)

    # How long a pooled PSRP runspace may sit idle before it is reopened rather
    # than reused. The runspace pool caches open WinRM sessions, but the *server*
    # decides how long its half lives: HTTP.SYS reaps an idle connection after
    # 120s by default, a GPO may shorten WinRM's own IdleTimeout, and a CredSSP
    # security context has its own lifetime. When any of those expire first, the
    # next command on that runspace gets an HTTP 401, and re-authenticating on a
    # half-dead connection fails outright — for CredSSP with
    # "Server did not response with a CredSSP token after step Credential
    # exchange". The failure is invisible until a request lands after an idle
    # gap, which is why it reads as "fails once, works on retry".
    #
    # Kept under the 120s HTTP.SYS default so the client gives up on a runspace
    # before the server does. PsrpTransport also retries once on a fresh
    # runspace, which covers the shorter timeouts this cannot predict.
    WINRM_RUNSPACE_MAX_IDLE_SECONDS: int = Field(default=60, ge=1)

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

        if self.WINRM_AUTH in ("ntlm", "credssp") and not (
            self.WINRM_USERNAME and self.WINRM_PASSWORD
        ):
            raise ValueError(
                f"WINRM_USERNAME and WINRM_PASSWORD are required when "
                f"WINRM_AUTH='{self.WINRM_AUTH}'. Prefer WINRM_AUTH='kerberos', "
                f"which authenticates from a keytab or credential cache with no "
                f"stored password — but note that kerberos cannot drive the "
                f"failover paths without delegation configured in AD."
            )

        return self


settings = Settings()
