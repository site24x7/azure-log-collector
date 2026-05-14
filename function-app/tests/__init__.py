"""Shared test fixtures for the function-app test suite."""

import os
import sys
import pytest

# Add function-app to sys.path so `shared` module is importable
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), os.pardir)
)
