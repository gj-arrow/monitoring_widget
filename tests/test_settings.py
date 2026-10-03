import json
import sys

import pytest

import theme
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
    original = Settings(x=100, y=200, alpha=0.65, always_on_top=False, log_history=True)
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


def test_a_non_utf8_file_falls_back_to_defaults(tmp_path):
    """A file written by an older build in cp1251 must not reach startup."""
    path = tmp_path / "settings.json"
    # ensure_ascii=False, or json would escape the Cyrillic and leave pure
    # ASCII that decodes as UTF-8 without complaint.
    path.write_bytes(json.dumps({"alpha": 0.5, "note": "спасибо"}, ensure_ascii=False).encode("cp1251"))
    assert load_settings(path) == Settings()


def test_a_utf16_file_falls_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(json.dumps({"alpha": 0.5}).encode("utf-16"))
    assert load_settings(path) == Settings()


def test_a_wrong_typed_field_keeps_its_valid_siblings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"x": "abc", "y": 200, "alpha": 0.5, "log_history": True}),
        encoding="utf-8",
    )
    assert load_settings(path) == Settings(x=None, y=200, alpha=0.5, log_history=True)


@pytest.mark.parametrize("alpha", [1.5, 0.34, 0.0, -1.0, "0.5", None])
def test_alpha_outside_the_usable_range_falls_back_to_the_default(tmp_path, alpha):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": alpha, "x": 10}), encoding="utf-8")
    loaded = load_settings(path)
    assert loaded == Settings(x=10)
    # The number, not Settings().alpha. Comparing a fallback against the
    # dataclass default it *is* the fallback from passes for any value at all:
    # change the default to 0.5 and this line still holds, so it was asserting
    # that the file was ignored and calling that a check on the alpha.
    assert loaded.alpha == 0.80


@pytest.mark.parametrize("alpha", [0.35, 1.0])
def test_alpha_at_the_range_boundary_is_accepted(tmp_path, alpha):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": alpha}), encoding="utf-8")
    assert load_settings(path).alpha == alpha


def test_alpha_as_an_integer_inside_the_range_is_normalised_to_float(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": 1}), encoding="utf-8")
    assert load_settings(path).alpha == 1.0
    assert isinstance(load_settings(path).alpha, float)


def test_alpha_that_is_not_a_number_is_rejected(tmp_path):
    for raw in ("NaN", "Infinity", "-Infinity"):
        path = tmp_path / "settings.json"
        path.write_text('{"alpha": %s}' % raw, encoding="utf-8")
        assert load_settings(path).alpha == Settings().alpha


@pytest.mark.parametrize("value", ['"abc"', "12.5", "true", "[1]"])
def test_a_non_int_position_is_dropped(tmp_path, value):
    path = tmp_path / "settings.json"
    path.write_text('{"x": %s, "y": 7}' % value, encoding="utf-8")
    assert load_settings(path) == Settings(y=7)


@pytest.mark.parametrize("value", ['"yes"', "1", "0", "null"])
def test_a_non_bool_toggle_is_dropped(tmp_path, value):
    path = tmp_path / "settings.json"
    path.write_text('{"always_on_top": %s, "log_history": true}' % value, encoding="utf-8")
    loaded = load_settings(path)
    assert loaded == Settings(always_on_top=True, log_history=True)
    # `1 == True` in Python, so equality alone would let an int through.
    assert loaded.always_on_top is True
    assert loaded.log_history is True


def test_an_explicit_null_position_is_a_legitimate_value(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"x": None, "y": 3}), encoding="utf-8")
    assert load_settings(path) == Settings(y=3)


def test_every_field_wrong_still_yields_usable_settings(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "x": "abc",
                "y": [],
                "alpha": "loud",
                "always_on_top": "yes",
                "log_history": None,
            }
        ),
        encoding="utf-8",
    )
    loaded = load_settings(path)
    assert loaded == Settings()
    assert isinstance(loaded.alpha, float)
    assert save_settings(loaded, path) is True
    assert load_settings(path) == Settings()


def test_a_settings_file_from_a_build_with_the_backdrop_still_loads(tmp_path):
    """The file on this machine still has "acrylic": false in it.

    Removing a persisted field must not cost the user the rest of their file:
    the loader skips keys it does not know, so an upgrade keeps the position and
    the opacity the user chose and silently forgets a setting that no longer
    exists. What it must *not* do is fail to load, or keep resurrecting the
    dead field.
    """
    from dataclasses import asdict, fields

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"x": 1229, "y": 10, "alpha": 0.65, "acrylic": True}),
        encoding="utf-8",
    )

    loaded = load_settings(path)

    assert loaded.alpha == 0.65 and loaded.x == 1229, (
        "a dead key cost the user a live one"
    )
    assert "acrylic" not in {f.name for f in fields(Settings)}, (
        "the backdrop is still a persisted setting: nothing can change it and "
        "nothing reads it, and the next writer puts it back in the file"
    )
    assert asdict(loaded) == asdict(Settings(x=1229, y=10, alpha=0.65))


def test_every_field_has_a_validator():
    """A field with no validator would be dropped on load, losing it silently."""
    from dataclasses import fields

    from settings import VALIDATORS

    assert set(VALIDATORS) == {f.name for f in fields(Settings)}


def test_a_dropped_field_is_logged_with_its_name(tmp_path, caplog):
    """Silently dropping a field would leave the user with no way to diagnose it."""
    import logging

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"alpha": "loud", "x": 5}), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="widget.settings"):
        loaded = load_settings(path)
    assert loaded == Settings(x=5)
    messages = [r.getMessage() for r in caplog.records if r.name == "widget.settings"]
    assert any("alpha" in m and "loud" in m for m in messages), messages


# --- the alpha range has one home -------------------------------------------


def test_the_alpha_range_is_declared_once_in_theme():
    """settings.py restated MIN_ALPHA/MAX_ALPHA, and nothing checked they agreed.

    Two copies of a bound are two answers to "what is the lowest usable
    opacity", and the disagreement would not show up as an error: the loader
    would reject a value the wheel had just produced, or accept one the panel
    clamps away. theme is the single home, so a value changed there is changed
    everywhere -- including the Settings dataclass default.
    """
    import settings as settings_module

    assert settings_module.MIN_ALPHA is theme.MIN_ALPHA
    assert settings_module.MAX_ALPHA is theme.MAX_ALPHA
    assert settings_module.Settings().alpha == theme.DEFAULT_ALPHA


def test_the_alpha_bounds_are_the_numbers_they_are():
    # Pinned rather than derived: these three decide what the panel can look
    # like and what a settings file is allowed to ask for, and a change to any
    # of them is a change to saved user data.
    assert (theme.MIN_ALPHA, theme.MAX_ALPHA, theme.DEFAULT_ALPHA) == (0.35, 1.0, 0.80)


def test_a_default_outside_the_range_would_be_rejected_by_the_loader(tmp_path):
    """The default has to satisfy the bound, or a fresh install cannot be saved.

    Settings.alpha is validated by the same _alpha() as any value read from
    disk, so a default outside [MIN_ALPHA, MAX_ALPHA] is a setting the app
    writes and then refuses to read back.
    """
    from settings import VALIDATORS

    check = VALIDATORS["alpha"]
    assert check(theme.DEFAULT_ALPHA) == theme.DEFAULT_ALPHA, (
        "theme.DEFAULT_ALPHA is outside the range the loader accepts, so every "
        "save of a default install writes a file that will not load"
    )


def test_deeply_nested_settings_do_not_reach_startup(tmp_path):
    """json.loads raises RecursionError on nesting, and that is not a ValueError.

    RecursionError is a RuntimeError, so the existing `except (OSError,
    ValueError)` did not cover it and the exception escaped load_settings --
    from a GUI startup path with no way to recover, which is exactly what that
    function's sanitising is for. A file is what a user or another program
    wrote; depth in it is not a reason to take the panel down.
    """
    from settings import load_settings

    path = tmp_path / "settings.json"
    path.write_text("[" * 20_000 + "]" * 20_000, encoding="utf-8")

    assert load_settings(path) == Settings()
