"""Compatibility shim: the vendor receiver now lives in imood/intake/vendor_ws.py (unchanged)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import imood.intake.vendor_ws as _m

sys.modules[__name__] = _m
