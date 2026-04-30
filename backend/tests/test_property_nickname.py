"""Property-based tests for nickname validation (task 2.3).

**Property 1: Nickname validation matches the stated rules.**

**Validates: Requirements 1.5, 1.6, 1.7.**

The pure validator ``app.domain.nickname.validate_nickname`` only enforces
the format-level rules (Requirements 1.5 and 1.6). The case-insensitive
uniqueness rule (Requirement 1.7) lives at the service layer and is
composed on top of the pure validator in production (task 3.1). To cover
all three requirements in one property test, this module defines a
small *service-shaped* acceptance predicate ``_accept`` that combines:

1. The pure validator's ``Ok`` outcome, and
2. A case-insensitive collision check against an injected "active
   player" set (modelled as a ``frozenset[str]`` of casefolded
   nicknames).

It then compares ``_accept`` against an independent reference predicate
``_spec_says_valid`` written directly from the acceptance-criteria text
(not by re-running the validator). The property is a bi-implication:
Hypothesis explores both the ACCEPT and REJECT halves of the decision,
which is why arbitrary Unicode text is mixed with a targeted
allowed-character strategy.

Out of scope here:
- Any production-code change. This module is pure test code.
- Scoring, leaderboard, game-state, or timeout properties (later tasks).
"""

from __future__ import annotations

import re
import string

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument

from app.domain.nickname import Ok, validate_nickname


# ---------------------------------------------------------------------------
# Hypothesis profile: stable, reproducible, CI-friendly.
# ---------------------------------------------------------------------------
#
# ``print_blob=True`` ensures failing examples are printed as a copyable
# reproducer blob. ``deadline=None`` keeps slow CI machines from
# triggering flaky deadline failures on a pure validator. The registration
# is guarded so re-imports (e.g., pytest collection quirks) don't raise
# ``InvalidArgument`` for a duplicate profile name.

try:
    settings.register_profile(
        "nickname-property",
        deadline=None,
        print_blob=True,
    )
except InvalidArgument:
    # Profile already registered (e.g., test module re-loaded). Safe to
    # ignore — the existing profile carries the same settings.
    pass

settings.load_profile("nickname-property")


# ---------------------------------------------------------------------------
# System under test (composed service-layer predicate).
# ---------------------------------------------------------------------------
#
# ``_accept`` mirrors what ``PlayerService.register`` will do in task
# 3.1: run the pure validator, and if it succeeds, reject when the
# casefolded form collides with any Active_Player's nickname_ci.


def _accept(nickname: str, active_ci: frozenset[str]) -> bool:
    """Service-layer acceptance predicate under test.

    Returns True if and only if the pure validator accepts ``nickname``
    AND the casefolded form does not appear in ``active_ci``.
    """
    result = validate_nickname(nickname)
    if not isinstance(result, Ok):
        return False
    return result.nickname_ci not in active_ci


# ---------------------------------------------------------------------------
# Reference predicate written directly from the acceptance criteria.
# ---------------------------------------------------------------------------
#
# Requirements 1.5, 1.6, 1.7 translated into an independent
# implementation that does NOT call ``validate_nickname``. The property
# ``_accept == _spec_says_valid`` only holds if both implementations
# agree, which catches regressions on either side.

_ALLOWED_RE = re.compile(r"\A[A-Za-z0-9 _\-]+\Z")


def _spec_says_valid(nickname: str, active_ci: frozenset[str]) -> bool:
    """Reference: does the spec say this nickname should be accepted?"""
    # Requirement 1.5: length in [2, 20].
    if not (2 <= len(nickname) <= 20):
        return False
    # Requirement 1.6: allowed characters only.
    if _ALLOWED_RE.fullmatch(nickname) is None:
        return False
    # Requirement 1.7: no case-insensitive collision with an active player.
    return nickname.casefold() not in active_ci


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------
#
# Coverage plan:
# - ``st.text()`` generates arbitrary Unicode (emoji, accented letters,
#   control chars, etc.). This heavily exercises the REJECT side of the
#   bi-implication, which matters because the property is "accept iff".
# - ``_targeted_text`` draws only from the allowed character set with a
#   length range that straddles the [2, 20] boundary ([0, 30]), so both
#   sides of the length rule AND the ACCEPT path are explored.
# - ``_nickname_and_active_set`` occasionally injects the candidate's
#   casefolded form into the active set so the collision branch fires
#   on otherwise-valid inputs, not only when Hypothesis happens to pick
#   overlapping text.

_ALLOWED_ALPHABET: list[str] = list(
    string.ascii_letters + string.digits + " _-"
)

_DISALLOWED_CHARS = st.characters().filter(
    lambda c: c not in _ALLOWED_ALPHABET
)


def _targeted_text(min_size: int = 0, max_size: int = 30) -> st.SearchStrategy[str]:
    """Text drawn from exactly the allowed character set.

    Size range straddles the length boundary (< 2 and > 20) so length
    rejection paths are exercised alongside the ACCEPT path.
    """
    return st.text(
        alphabet=st.sampled_from(_ALLOWED_ALPHABET),
        min_size=min_size,
        max_size=max_size,
    )


# Members of the active-player set must look like casefolded nickname_ci
# values. Using the allowed alphabet keeps the shrinker's output
# readable and reduces the chance that an active-set entry is not even
# a reachable casefold output.
_active_member_text = _targeted_text(min_size=2, max_size=20).map(str.casefold)


@st.composite
def _nickname_and_active_set(
    draw: st.DrawFn,
) -> tuple[str, frozenset[str]]:
    """Candidate nickname plus an active-player set.

    With roughly one-quarter probability the candidate's casefolded form
    is injected into the active set, guaranteeing that the collision
    branch is exercised on inputs that would otherwise be accepted.
    """
    candidate = draw(st.one_of(st.text(), _targeted_text()))
    active: set[str] = set(
        draw(st.frozensets(_active_member_text, max_size=8))
    )
    # Roughly 25% chance (two independent coin flips both True) to force
    # a collision on the current candidate. Two booleans are used so the
    # shrinker can drive the probability to 0 cleanly when the branch is
    # not the minimal reproducer.
    if draw(st.booleans()) and draw(st.booleans()):
        active.add(candidate.casefold())
    return candidate, frozenset(active)


# ---------------------------------------------------------------------------
# Combined bi-implication property.
# ---------------------------------------------------------------------------


@given(data=_nickname_and_active_set())
@settings(max_examples=200, deadline=None)
def test_accept_iff_rules_hold(data: tuple[str, frozenset[str]]) -> None:
    """Property 1: _accept(x) iff the spec rules hold on x.

    Validates: Requirements 1.5, 1.6, 1.7.
    """
    nickname, active_ci = data
    assert _accept(nickname, active_ci) == _spec_says_valid(nickname, active_ci)


# ---------------------------------------------------------------------------
# Directional properties (clearer shrinks when a single rule is broken).
# ---------------------------------------------------------------------------


@given(
    nickname=st.one_of(
        st.text(min_size=0, max_size=1),    # too short (0 or 1)
        st.text(min_size=21, max_size=40),  # too long (> 20)
    ),
    active_ci=st.frozensets(_active_member_text, max_size=4),
)
@settings(max_examples=200, deadline=None)
def test_rejects_all_invalid_lengths(
    nickname: str, active_ci: frozenset[str]
) -> None:
    """Any string with len ∉ [2, 20] is rejected regardless of charset/collisions.

    Validates: Requirement 1.5.
    """
    assert _accept(nickname, active_ci) is False


@given(
    prefix=_targeted_text(min_size=0, max_size=19),
    bad_char=_DISALLOWED_CHARS,
    suffix=_targeted_text(min_size=0, max_size=19),
    active_ci=st.frozensets(_active_member_text, max_size=4),
)
@settings(max_examples=200, deadline=None)
def test_rejects_any_string_with_disallowed_char(
    prefix: str,
    bad_char: str,
    suffix: str,
    active_ci: frozenset[str],
) -> None:
    """Any string containing a disallowed character is rejected.

    Construction guarantees at least one disallowed codepoint is
    present; length is bounded to keep the input within a realistic
    range but irrelevant to the assertion.

    Validates: Requirement 1.6.
    """
    nickname = prefix + bad_char + suffix
    assert _accept(nickname, active_ci) is False


@given(
    nickname=_targeted_text(min_size=2, max_size=20),
    active_ci=st.frozensets(_active_member_text, max_size=8),
)
@settings(max_examples=200, deadline=None)
def test_accepts_valid_nickname_when_no_collision(
    nickname: str, active_ci: frozenset[str]
) -> None:
    """A valid, non-colliding nickname is accepted.

    Validates: Requirements 1.5, 1.6 (positive path) and 1.7 (no-collision branch).
    """
    # Filter out the collision branch for this directional property.
    assume(nickname.casefold() not in active_ci)
    assert _accept(nickname, active_ci) is True


@given(
    nickname=_targeted_text(min_size=2, max_size=20),
    # An independent active set; the collision is forced below.
    extra_active=st.frozensets(_active_member_text, max_size=4),
    # Drive case perturbation: swap to upper, lower, casefold, or leave.
    case_op=st.sampled_from(
        (
            lambda s: s,
            str.upper,
            str.lower,
            str.casefold,
            str.swapcase,
        )
    ),
)
@settings(max_examples=200, deadline=None)
def test_rejects_on_case_insensitive_collision(
    nickname: str,
    extra_active: frozenset[str],
    case_op,
) -> None:
    """Case-insensitive collision with the active set rejects the nickname.

    The perturbed casing exercises Requirement 1.7's "case-insensitive"
    clause: regardless of how the active-player entry was originally
    cased, a casefolded collision still rejects.

    Validates: Requirement 1.7.
    """
    # Sanity-gate: the candidate must be format-valid, otherwise the
    # rejection would be attributable to 1.5 or 1.6 rather than 1.7.
    assume(_ALLOWED_RE.fullmatch(nickname) is not None)

    # Inject a collision derived from an arbitrary casing of the
    # candidate, casefolded to match the service-layer key.
    colliding_entry = case_op(nickname).casefold()
    active_ci = frozenset(extra_active | {colliding_entry})

    assert _accept(nickname, active_ci) is False
