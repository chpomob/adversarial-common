"""Pytest bootstrap: put the skill root on sys.path so the
``adversarial_common`` package is importable from the tests."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
