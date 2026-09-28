from fastapi import APIRouter, status

from app.services import dhcp_service
from app.utils.decorators import log_call

router = APIRouter(tags=["health"])


# Deliberately NOT behind verify_token, unlike every other route.
#
# This is the Deployment's readiness probe, and the kubelet sends no
# Authorization header — it cannot, since a probe has no way to read a Secret.
# With auth enforced here, every probe got a 401, the pod never became ready and
# it never joined the Service. Observed exactly that on the release that first
# generated a token.
#
# Safe because the response carries nothing worth protecting: check_health()
# returns a bare {"status": "ok"} or raises, so the only thing an anonymous
# caller learns is whether the DHCP server is currently reachable.
#
# The alternative — a tcpSocket readiness probe — was rejected: it would answer
# "is the process up" instead of "can this pod reach the DHCP server", and so
# would keep a pod that cannot serve anything in the Service endpoints.
@router.get("/healthz", status_code=status.HTTP_200_OK)
@log_call
async def healthz():
    return await dhcp_service.check_health()
