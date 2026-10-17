# SPDX-License-Identifier: Apache-2.0
"""Direct job text has a reviewed, exhaustive mapping to exact source steps."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "tools.recover-live", Path(__file__).with_name("recover-live.py")
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)

RAW = "\ufeff2026-01-01T00:00:00Z first\nplain\t\x1b[31mtext\x1b[m\n\n2025-01-01T00:00:00Z last\n"
HELP = (
    "Makes an authenticated HTTP request to the GitHub API and prints the response.\n"
    "\nUSAGE\n  gh api <endpoint> [flags]\n\nFLAGS\n"
    "      --verbose   Include full HTTP request and response in the output\n"
    "\nINHERITED FLAGS\n  --help   Show help for command\n"
)
ESCAPE = "      --allow-escape-sequences   Allow printing terminal escape sequences\n"


def steps_digest(steps):
    projection = [
        {key: step[key] for key in ("number", "name", "status", "conclusion")}
        for step in steps
    ]
    return recovery.digest(
        json.dumps(
            projection, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
    )


def representation():
    steps = [
        {
            "number": number,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
        }
        for number, name, conclusion in (
            (1, "Set up job", "success"),
            (2, "not run", "skipped"),
            (12, "same download name", "success"),
            (13, "same download name", "cancelled"),
        )
    ]
    spans = [
        {
            "number": 1,
            "name": "Set up job",
            "start": 1,
            "count": 1,
            "sha256": recovery.digest("\ufeff2026-01-01T00:00:00Z first\n".encode()),
        },
        {
            "number": 12,
            "name": "same download name",
            "start": 2,
            "count": 2,
            "sha256": recovery.digest("plain\t\x1b[31mtext\x1b[m\n\n".encode()),
        },
        {
            "number": 13,
            "name": "same download name",
            "start": 4,
            "count": 1,
            "sha256": recovery.digest(b"2025-01-01T00:00:00Z last\n"),
        },
    ]
    return {
        "source_log_sha256": recovery.digest(RAW.encode()),
        "source_job_steps_sha256": steps_digest(steps),
        "source_job_name": "real AWS",
        "source_log_spans": spans,
    }, {"steps": steps}


def test_annotation_preserves_bom_ansi_tabs_blank_lines_and_duplicate_step_names():
    manifest, job = representation()
    job["steps"][0]["started_at"] = "ignored"
    assert recovery.annotate_source_log(RAW, manifest, job) == (
        "real AWS\tSet up job\t\ufeff2026-01-01T00:00:00Z first\n"
        "real AWS\tsame download name\tplain\t\x1b[31mtext\x1b[m\n"
        "real AWS\tsame download name\t\n"
        "real AWS\tsame download name\t2025-01-01T00:00:00Z last\n"
    )


def test_canonical_steps_escape_non_ascii_and_accept_maximum_step_number():
    steps = [
        {
            "number": 2**31 - 1,
            "name": "café",
            "status": "completed",
            "conclusion": "success",
        }
    ]
    assert recovery.source_steps({"steps": steps}, steps_digest(steps)) == [
        (2**31 - 1, "café")
    ]


@pytest.mark.parametrize("change", ["empty", "status", "conclusion", "over-max"])
def test_step_schema_rejects_even_a_matching_digest(change):
    _, job = representation()
    if change == "empty":
        job["steps"] = []
    elif change == "over-max":
        job["steps"][-1]["number"] = 2**31
    else:
        job["steps"][0][change] = "in_progress" if change == "status" else None
    with pytest.raises(recovery.Refused, match="^source_steps_schema$"):
        recovery.source_steps(job, steps_digest(job["steps"]))


@pytest.mark.parametrize(
    "raw",
    [
        "",
        RAW[:-1],
        RAW + "\n",
        RAW[1:],
        RAW.replace("2026", "2027"),
        RAW.replace("\n", "\r\n"),
    ],
)
def test_raw_digest_is_checked_before_step_metadata_or_annotation(raw):
    manifest, job = representation()
    with patch.object(recovery, "source_steps") as steps:
        with pytest.raises(recovery.Refused, match="^source_log_digest$"):
            recovery.annotate_source_log(raw, manifest, job)
        steps.assert_not_called()


@pytest.mark.parametrize("raw", ["", "unterminated", "line\r\n", "line\rmore\n"])
def test_reviewed_text_requires_lf_only_and_final_lf(raw):
    manifest, job = representation()
    manifest["source_log_sha256"] = recovery.digest(raw.encode())
    with pytest.raises(recovery.Refused, match="^source_log_lines$"):
        recovery.annotate_source_log(raw, manifest, job)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        [],
        {},
        1,
        True,
        "x\tstep",
        "x\nstep",
        "x\rstep",
        "x\0step",
        "x\x1bstep",
        "x\x7fstep",
        "x\x85step",
        "x\u2028step",
        "x\u2029step",
    ],
)
def test_step_labels_cannot_inject_columns_or_lines(value):
    assert not recovery.source_label(value)
    manifest, job = representation()
    job["steps"][0]["name"] = value
    with pytest.raises(recovery.Refused, match="^source_steps_schema$"):
        recovery.annotate_source_log(RAW, manifest, job)
    manifest, job = representation()
    manifest["source_job_name"] = value
    with pytest.raises(recovery.Refused, match="^source_spans_schema$"):
        recovery.annotate_source_log(RAW, manifest, job)


@pytest.mark.parametrize(
    "point,allowed",
    [
        (31, False),
        (32, True),
        (126, True),
        (127, False),
        (159, False),
        (160, True),
        (8231, True),
        (8232, False),
        (8233, False),
        (8234, True),
    ],
)
def test_step_label_character_boundaries(point, allowed):
    assert recovery.source_label(chr(point)) is allowed


@pytest.mark.parametrize("value", [0, -1, True, False, 1.0, "1", None, 2**31])
def test_step_numbers_are_strict_bounded_positive_integers(value):
    manifest, job = representation()
    job["steps"][0]["number"] = value
    with pytest.raises(recovery.Refused, match="^source_steps_schema$"):
        recovery.annotate_source_log(RAW, manifest, job)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "duplicate",
        "order",
        "renamed",
        "skipped",
        "added",
        "status",
        "conclusion",
        "not-list",
        "empty",
        "not-dict",
    ],
)
def test_complete_step_list_is_pinned_including_skipped_steps(change):
    manifest, job = representation()
    steps = job["steps"]
    if change == "missing":
        steps.pop(1)
    elif change == "duplicate":
        steps[1]["number"] = 1
    elif change == "order":
        steps.reverse()
    elif change == "renamed":
        steps[1]["name"] = "renamed skipped step"
    elif change == "skipped":
        steps[0]["conclusion"] = "skipped"
    elif change == "added":
        steps.append({**steps[-1], "number": 14})
    elif change == "status":
        steps[1]["status"] = "in_progress"
    elif change == "conclusion":
        steps[1]["conclusion"] = None
    elif change == "not-list":
        job["steps"] = {}
    elif change == "empty":
        job["steps"] = []
    else:
        steps[1] = None
    with pytest.raises(recovery.Refused, match="^source_steps_(schema|digest)$"):
        recovery.annotate_source_log(RAW, manifest, job)


@pytest.mark.parametrize(
    "field,value",
    [
        ("start", True),
        ("start", 1.0),
        ("start", 0),
        ("start", 2),
        ("start", -1),
        ("start", 2**63),
        ("count", True),
        ("count", 1.0),
        ("count", 0),
        ("count", -1),
        ("count", 2**63),
        ("count", 5),
    ],
)
def test_span_bounds_reject_types_ranges_leading_gaps_and_overruns(field, value):
    manifest, job = representation()
    manifest["source_log_spans"][0][field] = value
    with pytest.raises(recovery.Refused, match="^source_spans_bounds$"):
        recovery.annotate_source_log(RAW, manifest, job)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("gap", "bounds"),
        ("overlap", "bounds"),
        ("trailing", "coverage"),
        ("missing", "schema"),
        ("extra", "schema"),
        ("mapping", "step"),
        ("order", "step"),
        ("number-bool", "step"),
        ("schema", "schema"),
        ("not-dict", "schema"),
        ("not-list", "schema"),
        ("digest", "digest"),
    ],
)
def test_spans_are_exact_exhaustive_nonoverlapping_step_slices(change, reason):
    manifest, job = representation()
    spans = manifest["source_log_spans"]
    if change == "gap":
        spans[1]["start"] += 1
    elif change == "overlap":
        spans[1]["start"] -= 1
    elif change == "trailing":
        raw = RAW + "unattributed\n"
        manifest["source_log_sha256"] = recovery.digest(raw.encode())
    elif change == "missing":
        spans.pop()
    elif change == "extra":
        spans.append(copy.deepcopy(spans[-1]))
    elif change == "mapping":
        spans[0]["name"] = "same download name"
    elif change == "order":
        spans.reverse()
    elif change == "number-bool":
        spans[0]["number"] = True
    elif change == "schema":
        spans[0]["extra"] = 1
    elif change == "not-dict":
        spans[0] = None
    elif change == "not-list":
        manifest["source_log_spans"] = {}
    else:
        spans[0]["sha256"] = "0" * 64
    code = "source_span_digest" if reason == "digest" else "source_spans_" + reason
    with pytest.raises(recovery.Refused, match="^" + code + "$"):
        recovery.annotate_source_log(
            raw if change == "trailing" else RAW, manifest, job
        )


@pytest.mark.parametrize("flag", [False, True])
def test_api_help_controls_escape_flag_only_on_captured_log_request(flag):
    help_text = HELP.replace("FLAGS\n", "FLAGS\n" + ESCAPE, 1) if flag else HELP
    client = recovery.Github("owner/repository", deadline=10, clock=lambda: 0)
    with patch.object(
        recovery.subprocess,
        "run",
        side_effect=[
            subprocess.CompletedProcess([], 0, help_text),
            subprocess.CompletedProcess([], 0, RAW),
        ],
    ) as run:
        assert client.log({"source_job_id": 42}) == RAW
    assert [call.args[0] for call in run.call_args_list] == [
        ["gh", "api", "--help"],
        [
            "gh",
            "api",
            "repos/owner/repository/actions/jobs/42/logs",
            *(["--allow-escape-sequences"] if flag else []),
        ],
    ]
    assert all(
        call.kwargs
        == {"capture_output": True, "text": True, "timeout": 10, "check": False}
        for call in run.call_args_list
    )


@pytest.mark.parametrize(
    "help_text",
    [
        "",
        "unknown",
        HELP.replace("gh api", "gh other"),
        HELP.replace("INHERITED FLAGS", "FLAGS"),
        HELP.replace(
            "FLAGS\n", "FLAGS\n" + ESCAPE.replace("   Allow", "=true   Allow"), 1
        ),
        HELP.replace("FLAGS\n", "FLAGS\n" + ESCAPE * 2, 1),
    ],
)
def test_unknown_or_ambiguous_api_help_fails_before_log_request(help_text):
    with patch.object(
        recovery.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, help_text),
    ) as run:
        with pytest.raises(recovery.Refused, match="^source_log_capability$"):
            recovery.Github("owner/repository").log({"source_job_id": 42})
    assert run.call_count == 1


def test_failed_or_deadline_consuming_api_help_never_fetches_log():
    for late in (False, True):
        times = iter([0, 10 if late else 0])
        client = recovery.Github(
            "owner/repository", deadline=10, clock=lambda: next(times)
        )
        with patch.object(
            recovery.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0 if late else 1, HELP),
        ) as run:
            with pytest.raises(
                recovery.Refused,
                match="^deadline$" if late else "^source_fetch_failed$",
            ):
                client.log({"source_job_id": 42})
        assert run.call_count == 1
