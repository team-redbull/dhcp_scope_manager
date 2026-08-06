"""Fixtures for the live-server integration suite.

Everything here exists to *undo* the mocking the unit suite relies on. The point
of these tests is that nothing between the router and Windows is faked.

The DHCP_IT=1 opt-in gate lives in the test module as a module-level marker, not
here: pytest_collection_modifyitems is a global hook, so a subdirectory conftest
defining it would skip the entire repository's tests, not just this directory.
"""
import pytest


@pytest.fixture(autouse=True)
def _bypass_dhcp_service_validation():
    """Override the parent conftest's autouse mock so validation really runs.

    tests/conftest.py patches validate_dhcp_environment for the whole suite, since
    the unit tests must pass on a machine with no DHCP server. Here that patch would
    hide exactly the failure mode worth catching — a misconfigured transport — so
    this same-named fixture shadows it and lets the real check execute.
    """
    from app.services import dhcp_service
    dhcp_service._reset_validation_cache()
    yield
    dhcp_service._reset_validation_cache()
