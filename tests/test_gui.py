"""The window: input checking, and driving real runs through it.

The window itself is created (hidden) so these tests exercise the real
buttons' code paths, with the run function stubbed.
"""

import time
import tkinter as tk

import pytest

from healthcare_loanscout import cli, gui


# --- input checking ---------------------------------------------------------------


def test_good_inputs_become_run_options():
    options = gui.options_from_inputs("PA", "191, 190", "50", dry_run=True)
    assert options.zip_prefixes == ("191", "190")
    assert options.fields is None, "every healthcare field is searched"
    assert options.limit == 50
    assert options.dry_run


def test_blank_zip_means_the_whole_state():
    assert gui.options_from_inputs("PA", "  ", "50", False).zip_prefixes == ()


@pytest.mark.parametrize(
    "zips,limit,message",
    [
        ("191", "0", "at least 1"),
        ("191", "lots", "whole number"),
        ("Philly", "50", "2 to 5 digits"),
        ("1", "50", "2 to 5 digits"),
    ],
)
def test_bad_inputs_get_a_plain_message(zips, limit, message):
    with pytest.raises(ValueError, match=message):
        gui.options_from_inputs("PA", zips, limit, False)


# --- the window ---------------------------------------------------------------------


@pytest.fixture
def app():
    root = tk.Tk()
    root.withdraw()
    window = gui.App(root)
    yield window
    root.destroy()


def _wait_until_idle(app, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.root.update()
        if app.worker is None and str(app.run_button["state"]) == "normal":
            return
        time.sleep(0.02)
    raise AssertionError("the window never finished the job")


def _log_text(app):
    return app.log.get("1.0", "end")


def test_no_field_picker_or_checkboxes(app):
    def widgets(w):
        yield w
        for child in w.winfo_children():
            yield from widgets(child)

    assert not hasattr(app, "field")
    assert not any(isinstance(w, gui.ttk.Checkbutton) for w in widgets(app.root))
    # The only drop-down left is the state list.
    assert [w for w in widgets(app.root) if isinstance(w, gui.ttk.Combobox)] == [app.state]


def test_preview_searches_every_field_and_reports(app, monkeypatch):
    seen = {}

    def fake_run(options, stop):
        seen["options"] = options
        cli.log.info("fake run wrote 3 rows")
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "run_leads", fake_run)
    app.start_run(dry_run=True)
    assert str(app.run_button["state"]) == "disabled", "buttons lock while running"
    _wait_until_idle(app)

    assert seen["options"].dry_run
    assert seen["options"].fields is None
    assert "fake run wrote 3 rows" in _log_text(app)
    assert app.status["text"] == "Done."


def test_stop_button_stops_the_job(app, monkeypatch):
    def fake_run(options, stop):
        stop.wait(10)
        return cli.EXIT_INTERRUPTED if stop.is_set() else cli.EXIT_OK

    monkeypatch.setattr(cli, "run_leads", fake_run)
    app.start_run(dry_run=False)
    app.root.update()
    started = time.monotonic()
    app.request_stop()
    _wait_until_idle(app)

    assert time.monotonic() - started < 3
    assert app.status["text"].startswith("Stopped")


def test_bad_input_shows_a_message_and_runs_nothing(app, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, text, parent: shown.append(text))
    monkeypatch.setattr(cli, "run_leads", lambda *a: pytest.fail("must not run"))
    app.zips.delete(0, "end")
    app.zips.insert(0, "1")

    app.start_run(dry_run=True)
    assert shown and "2 to 5 digits" in shown[0]
    assert app.worker is None


def test_a_crash_is_shown_not_swallowed(app, monkeypatch):
    def broken(options, stop):
        raise RuntimeError("something broke")

    monkeypatch.setattr(cli, "run_leads", broken)
    app.start_run(dry_run=True)
    _wait_until_idle(app)

    assert "something broke" in _log_text(app)
    assert "Couldn't start" in app.status["text"]


# --- branding --------------------------------------------------------------------------


def test_brand_styles_are_applied(app):
    assert str(app.run_button["style"]) == "Accent.TButton"
    assert str(app.preview_button["style"]) == "Primary.TButton"
    style = gui.ttk.Style(app.root)
    assert style.theme_use() == "clam", "Windows' native theme would ignore the colours"
    assert style.lookup("Accent.TButton", "background").upper() == gui.BRAND_TEAL


def test_window_still_opens_without_the_logo(monkeypatch, tmp_path):
    monkeypatch.setattr(gui, "LOGO_FILE", tmp_path / "missing.png")
    monkeypatch.setattr(gui, "ICON_FILE", tmp_path / "missing.png")
    root = tk.Tk()
    root.withdraw()
    try:
        window = gui.App(root)
        assert window.logo is None
        assert window.run_button.winfo_exists()
    finally:
        root.destroy()
