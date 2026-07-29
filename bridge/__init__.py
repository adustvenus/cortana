"""Cortana Mobile Bridge package.

The Cortana checkout root is put on sys.path here, once, so every bridge module
can import the top-level modules it wraps (config, hud_state, calendar_state,
orchestrator...) regardless of which bridge module gets imported first.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
