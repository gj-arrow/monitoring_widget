import json
import sys

from settings import Settings, load_settings, save_settings


def test_defaults_when_the_file_is_missing(tmp_path):
    assert load_settings(tmp_path / "nope.json") == Settings()


def test_defaults_when_the_file_is_corrupt(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_settings(path) == Settings()


def test_unknown_keys_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": 0.5, "from_the_future": 1}), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded.alpha == 0.5
    assert not hasattr(loaded, "from_the_future")


def test_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    original = Settings(x=100, y=200, alpha=0.65, always_on_top=False, acrylic=True, log_history=True)
    assert save_settings(original, path) is True
    assert load_settings(path) == original


def test_save_failure_is_reported_not_raised(tmp_path):
    unwritable = tmp_path / "missing-dir" / "settings.json"
    assert save_settings(Settings(), unwritable) is False


def test_config_path_ends_with_the_expected_filename():
    from settings import CONFIG_NAME, config_path

    assert config_path().name == CONFIG_NAME == "settings.json"


def test_config_path_anchors_on_the_executable_when_frozen(tmp_path, monkeypatch):
    """A frozen build unpacks into a temp dir wiped on exit, so __file__ is no use."""
    from settings import CONFIG_NAME, config_path

    exe = tmp_path / "dist" / "Monitor.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    assert config_path() == exe.resolve().parent / CONFIG_NAME
