"""Pytest config for the integration test suite.

The integration tests live in ``backend/tests/integration/``.
Pytest's default rootpath/import behavior puts the test file's
directory on ``sys.path``, but not the sibling ``tests/`` directory.
This conftest prepends the parent ``tests/`` directory so shared
helpers like ``api_helpers`` remain importable from here if future
integration tests want to reuse them.

The current suite is self-contained, but keeping this adjustment in
place means new integration tests can `import api_helpers` without
friction.
"""

from __future__ import annotations

import os
import sys


_TESTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
