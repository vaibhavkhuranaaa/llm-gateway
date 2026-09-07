"""Refuse to run the suite against a live Firestore project.

`services.identity` only supplies anonymous credentials when
`FIRESTORE_EMULATOR_HOST` is set. Without it the Google client falls back to
ambient credentials and the Firestore-backed tests reach
`firestore.googleapis.com` for real, using the throwaway project name each test
generates. They fail there with `PermissionDenied` rather than passing quietly,
but the request still leaves the machine, and an operator whose credentials do
reach those project names would be writing to a live project from a test run.

`scripts/run_m7_load.py` already refuses on the same condition. This applies
the same rule to the suite.
"""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        raise pytest.UsageError(
            "FIRESTORE_EMULATOR_HOST is required; the suite must not reach a live "
            "Firestore project. Start the emulator and re-run, for example: "
            "gcloud emulators firestore start --host-port=127.0.0.1:8787 and then "
            "FIRESTORE_EMULATOR_HOST=127.0.0.1:8787 uv run pytest"
        )
