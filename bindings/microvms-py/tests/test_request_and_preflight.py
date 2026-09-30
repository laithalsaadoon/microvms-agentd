# SPDX-License-Identifier: Apache-2.0
"""`SizeClass.from_request` and `preflight` (#223; BIND-14, BIND-15, BIND-16).

`from_request` is a pure selection over the documented table, so every boundary is testable
here. `preflight` needs AWS for its passing path; the live suite runs it against the real
account (`drive_preflight`). What a unit run can assert is the report's shape and the path
that makes no call at all: an environment region the client refuses stops the preflight at
the region check, and the credentials and service checks are reported as not run.
"""

from __future__ import annotations

import pytest

import microvms

SizeClass = microvms.SizeClass


@pytest.mark.parametrize(
    ("cpus", "memory_mib", "baseline"),
    [
        (0.25, 512, 512),
        (1.0, 2048, 2048),
        (4.0, 8192, 8192),
        (None, 1024, 1024),
        (0.5, None, 1024),
        (2.0, 1024, 4096),
        (0.5, 3072, 4096),
        (0.25, 513, 1024),
    ],
)
def test_the_smallest_class_whose_baseline_covers_both_axes(
    cpus: float | None, memory_mib: int | None, baseline: int
) -> None:
    """BIND-14: exact baseline matches, one axis unset, and just-over moving up a class."""
    assert SizeClass.from_request(cpus, memory_mib).baseline_mib == baseline
    assert (
        SizeClass.from_request(cpus=cpus, memory_mib=memory_mib).baseline_mib
        == baseline
    )


def test_a_request_that_names_nothing_is_the_default_class() -> None:
    """BIND-14: both unset (or zero) is the platform default, not the smallest class."""
    default = SizeClass.default_class().baseline_mib
    assert SizeClass.from_request().baseline_mib == default
    assert SizeClass.from_request(None, None).baseline_mib == default
    assert SizeClass.from_request(0, 0).baseline_mib == default


@pytest.mark.parametrize(
    ("cpus", "memory_mib"), [(4.5, None), (None, 8193), (16.0, 32768)]
)
def test_a_request_over_the_largest_class_names_it(
    cpus: float | None, memory_mib: int | None
) -> None:
    """BIND-14: a typed refusal naming the largest class."""
    largest = SizeClass.all()[-1]
    with pytest.raises(microvms.InvalidArgError, match="largest size class") as caught:
        SizeClass.from_request(cpus, memory_mib)
    assert largest.describe() in str(caught.value)


def test_a_cpu_figure_that_is_not_a_quantity_is_refused() -> None:
    with pytest.raises(microvms.InvalidArgError):
        SizeClass.from_request(float("nan"), None)
    with pytest.raises(microvms.InvalidArgError):
        SizeClass.from_request(-1.0, None)


def test_a_refused_environment_region_stops_the_preflight_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIND-15 and BIND-16: no region to check, so nothing after it runs or calls AWS."""
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    report = microvms.preflight()
    assert report.ok is False
    assert report.region is None
    names = [check.name for check in report.checks]
    assert names == ["region", "credentials", "service"]
    region = report.check("region")
    assert region is not None
    assert region.ok is False and region.fatal and region.ran
    assert "eu-central-1" in region.detail
    for name in ("credentials", "service"):
        check = report.check(name)
        assert check is not None
        assert (check.ok, check.fatal, check.ran) == (False, True, False)
    assert report.check("nope") is None
    as_dict = report.to_dict()
    assert as_dict["ok"] is False
    assert [check["name"] for check in as_dict["checks"]] == names
    assert "PreflightReport(ok=False" in repr(report)
