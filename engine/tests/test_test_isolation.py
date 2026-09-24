# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""The suite must not reach the developer's real ``~/.laya`` or keychain.

``laya/config.py`` resolves ``LAYA_HOME`` from ``Path.home()`` at import time,
and several fixtures call ``load_settings()`` / ``save_settings()`` and
``delete_mcp_token()``. Before ``conftest.py`` redirected ``HOME``, a plain
``pytest`` run rewrote ``~/.laya/settings.json``. The OS keychain is a separate
problem: it is *not* under ``HOME``, so the redirect did nothing for it and the
same run went on deleting the developer's real MCP bearer token — which presents
as "the token stopped working" to anyone who had Laya running before they ran
the tests.

``conftest.py`` now defends both stores, before the first ``laya`` import:
``HOME``/``USERPROFILE`` for the config directory and ``keyring.set_keyring`` for
the process keychain. These tests are what keep that true. The redirects have to
happen before ``config`` is imported, so moving the imports back above them, or
importing ``laya.config`` from another plugin earlier, would silently put the
suite back on the real directory.
"""

from __future__ import annotations

import os
import pathlib


def test_the_suite_runs_against_a_temporary_home():
    """HOME must be the throwaway directory conftest created, not the real one."""
    home = pathlib.Path(os.environ["HOME"]).resolve()

    assert home.name.startswith("laya-test-home"), (
        f"HOME is {home}, which is not the test directory conftest creates. "
        "The redirect in tests/conftest.py runs before the first `laya` import; "
        "if this fails, something imported laya.config earlier and the suite is "
        "writing to the developer's real ~/.laya."
    )


def test_settings_are_written_inside_the_temporary_home():
    """The path the code actually writes to must be under that temporary HOME.

    The path is checked *before* ``save_settings`` is called. Checking after the
    write would mean that on the failure this test exists to catch, the suite had
    already written to the developer's real settings file — the assertion would
    report the damage rather than prevent it.
    """
    from laya import config

    home = pathlib.Path(os.environ["HOME"]).resolve()
    config_file = pathlib.Path(config.LAYA_CONFIG_FILE).resolve()

    assert config_file.is_relative_to(home), (
        f"settings would be written to {config_file}, outside {home}. A pytest "
        "run must not be able to modify a real Laya installation."
    )

    from laya.config import load_settings, save_settings

    save_settings(load_settings())
    assert config_file.exists()


def test_the_suite_uses_an_in_memory_keychain():
    """The process keychain must be the test backend, not the developer's.

    Asserting the backend's identity is the point, not a proxy for it: no test
    can prove the real keychain was untouched without reading it, and reading it
    is exactly what the suite must not do. If the backend is ours, the real one
    is not reachable from any ``laya`` code path in this process.
    """
    import keyring

    backend = keyring.get_keyring()
    assert type(backend).__name__ == "_InMemoryKeyring", (
        f"keychain backend is {backend!r}. A fixture calling delete_mcp_token() "
        "would delete the developer's real MCP bearer token."
    )


def test_mcp_token_round_trips_through_the_test_keychain():
    """The engine's own store/read/delete path must work and stay in-process."""
    from laya.security.keychain import (
        delete_mcp_token as engine_delete,
        get_mcp_token as engine_get,
        store_mcp_token as engine_store,
    )

    assert engine_store("lyat_isolation_probe")
    try:
        assert engine_get() == "lyat_isolation_probe"
    finally:
        assert engine_delete()
    assert engine_get() is None


def test_a_second_concurrent_run_is_refused(tmp_path):
    """The test HOME is shared on purpose (warm ChromaDB cache), so a second live
    run must be refused rather than left to overwrite this one's settings. Two
    concurrent runs sharing one HOME were measured to race on ``settings.json``:
    one suite raised ``json.decoder.JSONDecodeError`` from a ``load_settings()``
    that read the file mid-write, and both ran ~1.75x slower. Failing loudly here
    is the difference between a five-second fix and a bisect."""
    import subprocess
    import sys

    import pytest

    from tests import conftest

    # A live process that is NOT this one — the guard deliberately ignores its own
    # pid, so the marker has to name a genuinely separate run.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert conftest._pid_alive(other.pid)
        (tmp_path / conftest._RUN_MARKER).write_text(str(other.pid))

        with pytest.raises(RuntimeError, match="another Laya test run"):
            conftest._acquire_run_marker(str(tmp_path))
    finally:
        other.kill()
        other.wait()


def test_a_stale_marker_is_taken_over(tmp_path):
    """A crashed run must not wedge the suite. Its pid is gone, so the marker is
    taken over instead of blocking every later run."""
    import subprocess
    import sys

    from tests import conftest

    dead = subprocess.Popen([sys.executable, "-c", ""])
    dead.wait()
    assert not conftest._pid_alive(dead.pid)

    marker = tmp_path / conftest._RUN_MARKER
    marker.write_text(str(dead.pid))

    conftest._acquire_run_marker(str(tmp_path))  # must not raise
    assert marker.read_text() == str(os.getpid())


def test_the_running_suite_holds_the_marker():
    """And the guard is actually wired up: a run that reaches the tests owns the
    marker in the HOME the code is using."""
    from pathlib import Path

    from tests import conftest

    marker = Path(os.environ["HOME"]) / conftest._RUN_MARKER
    assert marker.read_text() == str(os.getpid())


