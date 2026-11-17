"""Smoke test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from main import app  # noqa

def test_app_has_routes():
    assert len([r.path for r in app.routes]) >= 1
