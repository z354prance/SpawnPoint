import pytest

from app import config, main


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test logs activity into its own folder, and no test's auth override leaks into the next."""
    monkeypatch.setattr(config, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(config, "_host_gb", 256.0)
    from app import hostinfo
    hostinfo._guard.update(level="ok", where="", free_gb=None)          # disks start out fine in every test
    from app import state
    monkeypatch.setattr(state, "_path", str(tmp_path / "state" / "state.json"))  # never the real /servers
    (tmp_path / "state").mkdir(exist_ok=True)
    yield
    main.app.dependency_overrides.clear()
