"""Structured logging must scrub secrets from fields and tracebacks, not just msg."""

from __future__ import annotations

import io
import json
import logging

import pytest

from core import logging as app_logging


def _capture(json_output: bool) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    app_logging.configure_logging("INFO", json_output=json_output, stream=stream)
    return stream, logging.getLogger("tests.redaction")


def _log_with_every_leak(logger: logging.Logger) -> None:
    try:
        raise RuntimeError("boom with supersecret-token in the traceback")
    except RuntimeError:
        logger.error(
            "send failed for supersecret-token",
            extra={"url": "https://api.telegram.org/botsupersecret-token/sendMessage"},
            exc_info=True,
        )


def test_json_output_redacts_message_extra_and_traceback():
    app_logging.clear_secrets()
    app_logging.register_secret("supersecret-token")
    stream, logger = _capture(json_output=True)
    try:
        _log_with_every_leak(logger)
        line = stream.getvalue().strip().splitlines()[-1]
    finally:
        app_logging.clear_secrets()

    assert "supersecret-token" not in line
    payload = json.loads(line)
    assert app_logging.REDACTED in payload["msg"]
    assert app_logging.REDACTED in payload["url"]
    assert app_logging.REDACTED in payload["exc"]


def test_console_output_redacts_tracebacks_too():
    app_logging.clear_secrets()
    app_logging.register_secret("supersecret-token")
    stream, logger = _capture(json_output=False)
    try:
        _log_with_every_leak(logger)
        text = stream.getvalue()
    finally:
        app_logging.clear_secrets()

    assert "supersecret-token" not in text
    assert app_logging.REDACTED in text


@pytest.mark.parametrize("secret", [
    'token"with-quotes',
    "token\\with\\slashes",
    "token\nwith\ttabs",
    "token-雪🔑",
    "false, true",
    'ERROR", "logger": "tests.redaction',
])
@pytest.mark.parametrize("json_output", [True, False])
def test_redaction_preserves_escaped_values_and_json_structure(secret, json_output):
    """Feed the filter and formatter directly, without a logging call.

    Logging the fixture through ``logger.error`` is exactly what this test wants
    to prove is safe, but a static analyser cannot see that intent and reports a
    clear-text leak. The handler path is covered above, so here the record goes
    straight into the production filter and formatter.
    """

    class Unserializable:
        def __repr__(self):
            return secret

    record = logging.LogRecord(
        name="tests.redaction",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed: %s",
        args=(secret,),
        exc_info=(RuntimeError, RuntimeError(secret), None),
    )
    record.details = {"tokens": [secret], "flags": [False, True], "count": 2}
    record.fallback = Unserializable()

    app_logging.clear_secrets()
    app_logging.register_secret(secret)
    try:
        app_logging.SecretRedactionFilter().filter(record)
        formatter = app_logging.JsonFormatter() if json_output else app_logging.ConsoleFormatter()
        text = formatter.format(record)
    finally:
        app_logging.clear_secrets()

    if json_output:
        payload = json.loads(text)
        assert payload["level"] == "ERROR"
        assert payload["logger"] == "tests.redaction"
        assert payload["msg"] == "failed: " + app_logging.REDACTED
        assert payload["details"] == {"tokens": [app_logging.REDACTED], "flags": [False, True], "count": 2}
        assert payload["fallback"] == app_logging.REDACTED
        assert secret not in payload["exc"]
        assert app_logging.REDACTED in payload["exc"]
    else:
        assert secret not in text
        assert app_logging.REDACTED in text


def test_json_formatter_redacts_metadata_and_extra_keys():
    secret = 'token"with\\escapes'
    record = logging.LogRecord(secret, logging.INFO, __file__, 1, "safe", (), None)
    setattr(record, secret, {"nested": [secret]})
    app_logging.clear_secrets()
    app_logging.register_secret(secret)
    try:
        payload = json.loads(app_logging.JsonFormatter().format(record))
    finally:
        app_logging.clear_secrets()

    assert payload["logger"] == app_logging.REDACTED
    assert payload[app_logging.REDACTED] == {"nested": [app_logging.REDACTED]}


def test_short_values_are_not_registered():
    app_logging.clear_secrets()
    app_logging.register_secret("abc")
    assert app_logging.redact("abc is a common string") == "abc is a common string"


# -- the two environment settings -------------------------------------------


@pytest.fixture()
def root_logging_restored():
    """``configure_logging`` mutates the root logger; hand it back as found."""
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        root.setLevel(level)
        root.handlers[:] = handlers


def test_log_level_from_the_environment_is_applied(root_logging_restored, monkeypatch):
    """``LOG_LEVEL`` used to be parsed into Settings and then dropped.

    compose, the Docker image, the LaunchAgent and .env.example all set it. Nothing
    read it, because the CLI passed a non-None argparse default that pinned INFO.
    """
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    app_logging.configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.WARNING


def test_an_explicit_level_still_wins_over_the_environment(root_logging_restored, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    app_logging.configure_logging("INFO", stream=io.StringIO())

    assert logging.getLogger().level == logging.INFO


def test_a_bogus_log_level_falls_back_rather_than_crashing(root_logging_restored, monkeypatch):
    """A typo in an operator setting must not stop a service that is trying to log."""
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
    app_logging.configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.INFO
    assert app_logging.env_log_level("NOTICE") == "NOTICE"


def test_log_json_from_the_environment_is_applied(root_logging_restored, monkeypatch):
    monkeypatch.setenv("LOG_JSON", "true")
    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream)
    logging.getLogger("tests.env").info("cycle_finished")

    assert json.loads(stream.getvalue().strip())["msg"] == "cycle_finished"


def test_the_json_argument_pins_the_format(root_logging_restored, monkeypatch):
    monkeypatch.setenv("LOG_JSON", "true")
    stream = io.StringIO()
    app_logging.configure_logging("INFO", json_output=False, stream=stream)
    logging.getLogger("tests.env").info("cycle_finished")

    assert not stream.getvalue().lstrip().startswith("{")


def test_log_json_false_in_the_environment_is_applied(root_logging_restored, monkeypatch):
    """Both directions, because an implementation that always returned JSON would
    satisfy a one-sided test and silently break every operator who set the switch
    off to read logs by eye - the same asymmetry that hid LOG_LEVEL in the first
    place was only ever visible in one direction.
    """
    monkeypatch.setenv("LOG_JSON", "false")
    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream)
    logging.getLogger("tests.env").info("cycle_finished")

    assert not stream.getvalue().lstrip().startswith("{")
    assert "cycle_finished" in stream.getvalue()


def test_the_cli_leaves_both_settings_unset_so_the_environment_can_answer():
    """The regression itself, at the seam.

    ``store_true`` defaults to False and ``--log-level`` defaulted to "INFO": both
    non-None, so the environment was consulted for neither. A deployment could set
    LOG_JSON=true in three places and still get console logs.
    """
    from bot.main import build_parser

    unset = build_parser().parse_args([])
    assert unset.log_level is None
    assert unset.json_logs is None

    explicit = build_parser().parse_args(["--log-level", "ERROR", "--json-logs"])
    assert explicit.log_level == "ERROR"
    assert explicit.json_logs is True


# -- where the settings are written, and what words mean ---------------------


def test_values_written_in_the_dotenv_file_reach_the_logger(root_logging_restored, monkeypatch, tmp_path):
    """`.env` is where the manual tells an operator to put these settings.

    Reading them from ``os.environ`` is worthless if the file is loaded afterwards,
    and that ordering was the first fix's remaining hole: ``Settings.load()`` loads
    the file, and logging was configured before ``Settings.load()`` ran.
    """
    import config.settings as settings_module

    (tmp_path / ".env").write_text("LOG_LEVEL=WARNING\nLOG_JSON=false\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_JSON", raising=False)

    settings_module.load_dotenv_file()
    app_logging.configure_logging(stream=io.StringIO())

    assert logging.getLogger().level == logging.WARNING
    assert app_logging.env_flag("LOG_JSON", default=True) is False


@pytest.mark.parametrize("value", ["false", "0", "no", "n", "off"])
def test_every_false_spelling_disables_json(value, root_logging_restored, monkeypatch):
    """``n`` and ``off`` were accepted by Settings and not by the logger.

    One setting, two parsers, two vocabularies: the value was documented, parsed
    by the settings layer, and ignored by the layer that actually formats output.
    """
    monkeypatch.setenv("LOG_JSON", value)
    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream)
    logging.getLogger("tests.env").info("cycle_finished")

    assert not stream.getvalue().lstrip().startswith("{")


@pytest.mark.parametrize("value", ["true", "1", "yes", "y", "on"])
def test_every_true_spelling_enables_json(value, root_logging_restored, monkeypatch):
    monkeypatch.setenv("LOG_JSON", value)
    stream = io.StringIO()
    app_logging.configure_logging("INFO", stream=stream)
    logging.getLogger("tests.env").info("cycle_finished")

    assert json.loads(stream.getvalue().strip())["msg"] == "cycle_finished"


def test_logging_and_settings_agree_on_boolean_spellings():
    """The vocabularies are not allowed to drift apart silently."""
    from config import settings as settings_module

    assert app_logging.ENV_TRUE == frozenset(settings_module._ENV_TRUE)
    assert app_logging.ENV_FALSE == frozenset(settings_module._ENV_FALSE)
