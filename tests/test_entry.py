from __future__ import annotations

import sys

from gh_freshclone import entry


def test_version_fast_path_does_not_import_cli(capsys, monkeypatch) -> None:
    monkeypatch.delitem(sys.modules, "gh_freshclone.cli", raising=False)

    assert entry.main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == "0.11.0"
    assert "gh_freshclone.cli" not in sys.modules
