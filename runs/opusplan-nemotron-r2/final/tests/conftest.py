"""pytest configuration for livedoc tests."""

import sys
import os

# Prevent bytecode writing
sys.dont_write_bytecode = True

# Disable cache provider
os.environ["PYTEST_ADDOPTS"] = os.environ.get("PYTEST_ADDOPTS", "") + " --no-cacheprovider"
