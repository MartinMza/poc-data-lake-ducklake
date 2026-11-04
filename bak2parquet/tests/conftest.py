"""Tests sin motor: el paquete bajo `src/` entra al `sys.path`."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
