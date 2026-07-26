from collections.abc import Iterable
from pathlib import Path

import pytest
from packaging.requirements import Requirement


EXPECTED_URL = (
    "git+https://github.com/yakdoli/gwanbo-ocr.git"
    "@bfc350d815eb79bd726e35ddb765cf16692418f6"
)


def parse_gwanbo_ocr_requirement(lines: Iterable[str]) -> Requirement:
    candidates = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and "gwanbo-ocr" in line
    ]
    assert len(candidates) == 1, "requirements must contain exactly one gwanbo-ocr entry"

    requirement_text = candidates[0]
    assert not requirement_text.startswith("-e "), "editable requirements are not allowed"
    requirement = Requirement(requirement_text)
    assert requirement.name == "gwanbo-ocr"
    assert requirement.extras == {"pdf"}
    assert requirement.url == EXPECTED_URL
    return requirement


def test_requirements_pin_gwanbo_ocr_to_immutable_commit() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8").splitlines()

    parse_gwanbo_ocr_requirement(requirements)


@pytest.mark.parametrize(
    "requirement",
    [
        "-e ../../processing/ocr/gwanbo-ocr[pdf]",
        "gwanbo-ocr[pdf] @ git+https://github.com/yakdoli/gwanbo-ocr.git@main",
        "gwanbo-ocr[pdf] @ git+https://github.com/yakdoli/gwanbo-ocr.git@bfc350d",
    ],
)
def test_rejects_non_immutable_gwanbo_ocr_requirements(requirement: str) -> None:
    with pytest.raises(AssertionError):
        parse_gwanbo_ocr_requirement([requirement])
