"""
tests/conftest.py — Pytest Configuration for J.A.R.V.I.S. Test Suites.
"""

import os
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--generate-goldens",
        action="store_true",
        default=False,
        help="Generate or update visual regression golden images in tests/goldens/",
    )


@pytest.fixture(scope="session")
def generate_goldens_flag(pytestconfig):
    """Returns True if --generate-goldens was passed or GENERATE_GOLDENS=1 is in env."""
    return pytestconfig.getoption("--generate-goldens") or os.environ.get("GENERATE_GOLDENS") == "1"
