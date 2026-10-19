"""Every test runs against scratch trees, never this checkout: before any test
module loads, the package is pointed at an empty directory, so a test that
does not set up its own project, or a run outliving its test, touches nothing
real."""

import atexit
import tempfile
from pathlib import Path

from gci import core

_EMPTY = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
atexit.register(_EMPTY.cleanup)
core.ROOT = Path(_EMPTY.name).resolve()
