# SPDX-License-Identifier: Apache-2.0
"""Bounded control-plane reads for the live suite's independent observations."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from botocore.config import Config
from botocore.exceptions import ClientError

from harness.constants import SERVICE

T = TypeVar("T")
OBSERVATION_POLL_SECONDS = 2
# Every MicroVMs listing caps a page at 50 items (the service model's `maxResults`), and
# a listing given no page size returns about ten. With SDK retries off each page is one
# call, so at the default size a snapshot of a few hundred VMs is dozens of calls in a few
# seconds. That burst throttles itself, and `retry_observation` then starts the snapshot
# again from its first page, so a snapshot of a busy account can miss its whole deadline.
LISTING_PAGES = {"PageSize": 50}


def listing_pages(plane: Any, operation: str, **params: Any) -> Iterable[Any]:
    """Every page of one MicroVMs listing, at the service's largest page size."""
    return plane.get_paginator(operation).paginate(
        **params, PaginationConfig=LISTING_PAGES
    )


def bounded_plane(aws: Any) -> Any:
    # Retry deadlines belong to the observer. Bound individual calls too, so SDK retries
    # cannot keep a cleanup iteration alive after its deadline.
    return aws.client(
        SERVICE,
        config=Config(connect_timeout=5, read_timeout=5, retries={"max_attempts": 0}),
    )


def retry_observation(
    read: Callable[[], T],
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Retry a throttled observation, never substituting an empty or partial snapshot.

    The callback must rebuild its whole snapshot: a later page or explicit-id read can
    throttle after an earlier page succeeded. Other failures still reach the caller.
    """
    while True:
        try:
            return read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ThrottlingException":
                raise
            remaining = deadline - clock()
            if remaining <= 0:
                raise
            sleep(min(OBSERVATION_POLL_SECONDS, remaining))
            if clock() >= deadline:
                raise
