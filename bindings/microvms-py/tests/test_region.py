# SPDX-License-Identifier: Apache-2.0
"""The region surface: `src/region.rs`.

`Region.parse` takes any well-formed region name since 1.3.0 (#743), and `Region.measured()` lists
the 14 regions AWS was measured running MicroVMs in. TRAP-6 is why the list matters: a region
that does not run MicroVMs can answer `AccessDeniedException` with the message field **null**,
which an IAM denial also produces, so the error names the region's availability when the region
is outside the list.

`test_smoke.py` covers the headline case. This file covers the parts a single case cannot: that
`parse` checks the shape and nothing more, that the deprecated `unlisted`, `supported` and
`is_supported` keep their 1.2 behavior and warn, and that a `Region` behaves as a value, equality
and hashing, because a caller keys a pool by it.
"""

from __future__ import annotations

import pytest

import microvms

NAMED = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-northeast-1"]
MEASURED = [
    *NAMED,
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "eu-central-1",
    "eu-north-1",
    "ap-east-1",
    "ap-southeast-5",
    "ap-southeast-7",
    "eu-south-2",
]


def named_constructors() -> list[tuple[str, microvms.Region]]:
    """The five named doors, paired with the wire name each answers."""
    return [
        ("us-east-1", microvms.Region.us_east_1()),
        ("us-east-2", microvms.Region.us_east_2()),
        ("us-west-2", microvms.Region.us_west_2()),
        ("eu-west-1", microvms.Region.eu_west_1()),
        ("ap-northeast-1", microvms.Region.ap_northeast_1()),
    ]


# -- names and the measured list -----------------------------------------------


def test_each_named_constructor_answers_its_own_region_and_reports_measured() -> None:
    """Five constructors, five distinct regions, no two aliased.

    Aliasing is the mistake worth checking: `us_east_2()` returning us-east-1 would send every
    call to the wrong region's endpoint and nothing local would object. A test that only counted
    `measured()` would not see it.
    """
    built = named_constructors()
    assert [name for name, _ in built] == NAMED
    for name, region in built:
        assert region.name == name
        assert region.is_measured
        assert str(region) == name


def test_measured_is_the_fourteen_regions_starting_with_the_named_five() -> None:
    """The list AWS was measured running MicroVMs in, in launch order.

    The named constructors are its first five, so a region reachable by name is one the list
    reports as measured.
    """
    enumerated = [region.name for region in microvms.Region.measured()]
    assert enumerated == MEASURED
    assert enumerated[:5] == [name for name, _ in named_constructors()]
    assert all(region.is_measured for region in microvms.Region.measured())


@pytest.mark.parametrize("name", MEASURED)
def test_parse_accepts_every_measured_name_and_round_trips_it(name: str) -> None:
    """A measured name parses to a region that says it is measured.

    Round-tripped through the string form, which is what a config file round trip looks like.
    """
    parsed = microvms.Region.parse(name)
    assert parsed.name == name
    assert parsed.is_measured
    assert microvms.Region.parse(str(parsed)) == parsed


@pytest.mark.parametrize(
    "name", ["us-west-1", "ca-central-1", "us-gov-west-1", "me-south-1"]
)
def test_parse_accepts_a_well_formed_unmeasured_name_and_says_so(name: str) -> None:
    """Any well-formed name parses, and `is_measured` tells the two apart.

    us-west-1 answered `ListMicrovms` with the null-message denial on 2026-10-24, and the client
    still takes it: the error a call there produces names the region, which is where the
    warning now lives.
    """
    parsed = microvms.Region.parse(name)
    assert parsed.name == name
    assert not parsed.is_measured


@pytest.mark.parametrize(
    "name",
    [
        "US-EAST-1",  # right region, wrong case
        "us-east-1 ",  # a trailing space from a config file
        " us-east-1",
        "useast1",  # the dashes dropped
        "us_east_1",  # underscores, as an env-var name would spell it
        "",
        "local",
        "us-east-1.example.com",  # a name that would change the endpoint's host
    ],
)
def test_parse_refuses_a_malformed_name_without_normalising(name: str) -> None:
    """Exact, and that is deliberate rather than an oversight.

    A `parse` that trimmed or case-folded would accept two spellings of one region, and whichever
    consumer keyed on the raw string would split the group. The name becomes the middle of the
    endpoint's host, so anything outside a region name's shape is refused before it reaches a URL.
    """
    with pytest.raises(microvms.InvalidArgError) as raised:
        microvms.Region.parse(name)
    assert raised.value.code == "ERR_INVALID_ARG"
    assert raised.value.wire_kind is None, "nothing reached the daemon"


def test_the_refusal_names_the_shape_and_an_example() -> None:
    """The way out, so a caller can tell a typo from a misunderstanding."""
    with pytest.raises(microvms.InvalidArgError) as raised:
        microvms.Region.parse("EU-CENTRAL-1")
    message = str(raised.value)
    assert "EU-CENTRAL-1" in message
    assert "not a region name" in message
    assert "us-east-1" in message


# -- the deprecated 1.2 surface ------------------------------------------------


def test_unlisted_is_deprecated_and_builds_the_region_parse_does() -> None:
    """1.2 code keeps running, and says it should move.

    `unlisted` raises a `DeprecationWarning` and returns the same value `parse` would, so a
    region reached either way is one value.
    """
    with pytest.deprecated_call():
        hatched = microvms.Region.unlisted("us-west-1")
    assert hatched == microvms.Region.parse("us-west-1")
    assert not hatched.is_measured


def test_supported_and_is_supported_warn_and_keep_the_five() -> None:
    """The 1.2 meaning, one of the five named regions, with a warning that names the move.

    eu-central-1 is the case that separates the two predicates: AWS runs MicroVMs there, so it is
    measured, and it has no named constructor, so it was never supported.
    """
    with pytest.deprecated_call():
        enumerated = [region.name for region in microvms.Region.supported()]
    assert enumerated == NAMED
    with pytest.deprecated_call():
        assert microvms.Region.us_east_1().is_supported
    with pytest.deprecated_call():
        assert not microvms.Region.parse("eu-central-1").is_supported
    assert microvms.Region.parse("eu-central-1").is_measured


@pytest.mark.parametrize("name", NAMED)
def test_unlisted_gives_a_named_region_back_as_its_constructor_does(name: str) -> None:
    """One region, one value, however it was reached.

    Without this, `unlisted("us-east-1")` would be a *second* value for one region, equal to
    nothing and hashing differently, and anything keyed by region would hold two entries for one
    place.
    """
    with pytest.deprecated_call():
        hatched = microvms.Region.unlisted(name)
    assert hatched == microvms.Region.parse(name)
    assert hatched.is_measured
    assert hash(hatched) == hash(microvms.Region.parse(name))


def test_unlisted_carries_a_name_verbatim_as_1_2_did() -> None:
    """No shape check and no normalising on the deprecated path, as in 1.2."""
    for name in ["EU-CENTRAL-1", "some-future-region-9"]:
        with pytest.deprecated_call():
            assert microvms.Region.unlisted(name).name == name


# -- a region is a value -------------------------------------------------------


def test_two_calls_to_one_constructor_compare_equal_and_hash_alike() -> None:
    """Value semantics, which is what makes a region usable as a dict key.

    A pool keyed by region is the obvious use, and with reference equality every lookup would
    miss — silently building one entry per call rather than one per region.
    """
    for _, region in named_constructors():
        twin = microvms.Region.parse(region.name)
        assert region == twin
        assert hash(region) == hash(twin)
    # And as a real key: five regions, five slots, no collisions or duplicates.
    keyed = {region: region.name for _, region in named_constructors()}
    assert len(keyed) == 5
    assert keyed[microvms.Region.us_west_2()] == "us-west-2"


def test_two_different_regions_are_not_equal() -> None:
    """The other half of equality, so `__eq__` is not a constant.

    An `__eq__` returning `True` unconditionally would pass every assertion above.
    """
    regions = [region for _, region in named_constructors()]
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            assert first != second
    assert len({hash(region) for region in regions}) == 5


def test_an_unmeasured_region_is_not_equal_to_a_named_one() -> None:
    """Equality is by name, so an unmeasured region is a distinct key.

    A set holding us-west-1 and us-east-1 holds two regions, and a lookup by either finds its own.
    """
    unmeasured = microvms.Region.parse("us-west-1")
    for _, named in named_constructors():
        assert unmeasured != named
    assert len({unmeasured, microvms.Region.us_east_1()}) == 2


def test_the_repr_says_both_the_name_and_whether_it_is_measured() -> None:
    """A debug form that answers the question someone is debugging.

    A bare name in a traceback would leave the reader unable to tell an unmeasured region from a
    measured one — which is precisely what they are looking at the traceback to find out. So the
    assertion is on the *flag being distinguishable*, which is the property, rather than on the
    exact sentence.

    Note the boolean is Rust-spelled (`is_measured=true`, not `True`): the repr is built with
    `format!`, so it renders Rust's `Display` for a `bool`. Asserted as observed rather than as
    Python convention would have it, because a test written to the convention would be a test
    that fails against the shipped binding — and this is a debug string, not a parsed one.
    """
    measured = repr(microvms.Region.us_east_1())
    unmeasured = repr(microvms.Region.parse("us-west-1"))
    assert "us-east-1" in measured
    assert "is_measured=true" in measured
    assert "is_measured=false" in unmeasured
    # The property under test: the two are distinguishable at a glance.
    assert measured != unmeasured


def test_a_region_cannot_be_constructed_from_a_bare_string_by_calling_the_class() -> (
    None
):
    """No `__new__`, so an unchecked name is not writable.

    With a constructor, `Region("eu-central-1")` would be the shortest path in the module and it
    would bypass the shape check `parse` makes.
    """
    with pytest.raises(TypeError):
        microvms.Region("us-east-1")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        microvms.Region()  # type: ignore[call-arg]
