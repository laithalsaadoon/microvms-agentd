# SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from microvms import ControlPlane, InvalidArgError, Region, Sandbox


@pytest.mark.parametrize("kind", [ControlPlane, Sandbox])
def test_retry_budget_is_validated_before_credentials(kind):
    with pytest.raises(InvalidArgError) as raised:
        kind(Region.us_east_1(), retry=(0, 1.0))
    assert raised.value.attempt_count == 0
    assert raised.value.backoff_seconds == 0.0
    assert raised.value.throttled is False


@pytest.mark.parametrize("kind", [ControlPlane, Sandbox])
def test_async_retry_budget_is_validated_before_credentials(kind):
    async def run():
        with pytest.raises(InvalidArgError):
            await kind.create_async(Region.us_east_1(), retry=(1, 0.0))

    asyncio.run(run())
