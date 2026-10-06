import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import common  # noqa: E402
import demo  # noqa: E402


@pytest.fixture
def pf(tmp_path, monkeypatch):
    """A fresh demo tree under tmp_path with PF_ROOT and the fake identities set."""
    root = demo.build(tmp_path / "pf")
    monkeypatch.setenv("PF_ROOT", str(root))
    for k, v in demo.IDENTITY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(common, "_config_cache", {})
    # never touch the real Keychain from tests
    monkeypatch.setattr(common, "secret", lambda key: os.environ.get(common._env_name(key)))
    import importer
    monkeypatch.setattr(importer, "secret", common.secret)
    return root
