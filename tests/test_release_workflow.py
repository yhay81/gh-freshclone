from __future__ import annotations

import re
from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
PYPI_PUBLISH_ACTION_COMMIT = "ba38be9e461d3875417946c167d0b5f3d385a247"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_pypi_publisher_is_commit_pinned_and_generates_attestations() -> None:
    workflow = _workflow()
    matches = re.findall(
        r"uses: pypa/gh-action-pypi-publish@([0-9a-f]{40})",
        workflow,
    )

    assert matches == [PYPI_PUBLISH_ACTION_COMMIT]
    assert (
        f"pypa/gh-action-pypi-publish@{PYPI_PUBLISH_ACTION_COMMIT} # v1.14.1"
        in workflow
    )
    assert "          attestations: true\n" in workflow
    assert "uv publish" not in workflow


def test_pypi_publisher_receives_only_wheel_and_source_distribution() -> None:
    workflow = _workflow()

    assert "          mkdir pypi-dist\n" in workflow
    assert "          cp -- dist/*.whl dist/*.tar.gz pypi-dist/\n" in workflow
    assert "          packages-dir: pypi-dist/\n" in workflow
    assert "            dist/SHA256SUMS\n" in workflow
    assert "          files=(dist/*)\n" in workflow
    assert '          test "${#files[@]}" -eq 3\n' in workflow


def test_only_publish_job_receives_oidc_permission() -> None:
    workflow = _workflow()
    build_section, publish_section = workflow.split("\n  publish:\n", maxsplit=1)

    assert "id-token: write" not in build_section
    assert "      id-token: write\n" in publish_section
    assert publish_section.count("if: vars.PYPI_TRUSTED_PUBLISHING == 'true'") == 2
