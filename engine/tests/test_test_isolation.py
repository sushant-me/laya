# Copyright 2026 Aayush Chawla
# SPDX-License-Identifier: Apache-2.0

"""The suite must not reach the developer's real ``~/.laya``.

``laya/config.py`` resolves ``LAYA_HOME`` from ``Path.home()`` at import time,
and several fixtures call ``load_settings()`` / ``save_settings()`` and
``delete_mcp_token()``. Before ``conftest.py`` redirected ``HOME``, a plain
``pytest`` run rewrote ``~/.laya/settings.json`` and deleted the developer's MCP
bearer token from the OS keychain — which presents as "the token stopped
working" to anyone who had Laya running before they ran the tests.

``conftest.py`` sets ``HOME`` (and ``USERPROFILE``) to a throwaway directory
*before* the first ``laya`` import. This test is what keeps that true: the
redirect has to happen before ``config`` is imported, so moving the imports back
above it, or importing ``laya.config`` from another plugin earlier, would
silently put the suite back on the real directory.
"""

from __future__ import annotations

import os
import pathlib


def test_the_suite_runs_against_a_temporary_home():
    """HOME must be the throwaway directory conftest created, not the real one."""
    home = pathlib.Path(os.environ["HOME"]).resolve()

    assert home.name.startswith("laya-test-home-"), (
        f"HOME is {home}, which is not the temporary directory conftest creates. "
        "The redirect in tests/conftest.py runs before the first `laya` import; "
        "if this fails, something imported laya.config earlier and the suite is "
        "writing to the developer's real ~/.laya."
    )


def test_settings_are_written_inside_the_temporary_home():
    """The path the code actually writes to must be under that temporary HOME."""
    from laya import config
    from laya.config import load_settings, save_settings

    home = pathlib.Path(os.environ["HOME"]).resolve()

    save_settings(load_settings())

    config_file = pathlib.Path(config.LAYA_CONFIG_FILE).resolve()
    assert config_file.is_relative_to(home), (
        f"settings were written to {config_file}, outside {home}. A pytest run "
        "must not be able to modify a real Laya installation."
    )
    assert config_file.exists()
