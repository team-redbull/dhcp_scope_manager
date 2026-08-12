# DHCP Scope Manager API.
#
# Runs on Linux and drives a Windows DHCP server over PSRP (DHCP_TRANSPORT=psrp).
# No PowerShell is installed or needed here — pypsrp speaks the WinRM protocol
# directly and the cmdlets execute on the Windows host.
#
# Python 3.12, not 3.13: the pinned pydantic 2.8.0 / pydantic-core pair has no
# 3.13 wheels and would fall back to a source build.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Kerberos client libraries. Not needed for NTLM, but pypsrp requires them the
# moment WINRM_AUTH=kerberos — which is the production path (no stored password).
# Installing them here keeps the image identical between auth modes, so switching
# is a config change rather than a rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libkrb5-3 \
        krb5-user \
        libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# tests/ and scripts/ are deliberately NOT copied. They used to be, because the
# deployed API could run its own pytest suite via POST /api/v1/test-runs — that
# endpoint is gone, and with it the only thing in the image that read them. The
# runtime deps shrank the same way: pytest, pytest-asyncio and httpx moved out of
# requirements.txt into requirements-dev.txt.
#
# If an in-image suite is ever wanted again (the air-gapped side has no CI and no
# package index), all three have to come back together — sources, deps, endpoint.

# OpenShift's restricted-v2 SCC runs the container as an arbitrary UID from the
# namespace's range, NOT as the USER declared below, and always with GID 0. Files
# must therefore be group-owned by root and group-readable, or the process cannot
# read its own code. Declaring a non-root USER additionally satisfies
# runAsNonRoot on clusters that enforce it.
RUN chgrp -R 0 /app && chmod -R g=u /app

USER 1001

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
