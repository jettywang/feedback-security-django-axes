from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from django.utils import timezone

from axes.conf import settings
from axes.helpers import get_cool_off, get_failure_limit, get_lockout_tier
from axes.models import AccessAttempt, AccessAttemptExpiration

STATE_LOCKED = "locked"
STATE_EXPIRED = "expired"
STATE_FAILED = "failed"

STATISTIC_STATES = (STATE_LOCKED, STATE_EXPIRED, STATE_FAILED)


@dataclass(frozen=True)
class EffectiveRule:
    """
    The lockout rule that is actually in effect for an access attempt.

    ``threshold`` is the failure count that triggers the lockout and
    ``cooloff`` is the time the lockout stays in effect, or ``None`` for
    permanent lockouts.
    """

    threshold: int
    cooloff: Optional[timedelta]
    is_tier: bool

    @property
    def key(self) -> str:
        if self.is_tier:
            return f"tier:{self.threshold}"
        return f"limit:{self.threshold}"


@dataclass
class LockoutStatisticGroup:
    rule: EffectiveRule
    counts: dict = field(
        default_factory=lambda: {
            STATE_LOCKED: 0,
            STATE_EXPIRED: 0,
            STATE_FAILED: 0,
        }
    )


def resolve_effective_rule(failures: int) -> EffectiveRule:
    """Resolve the cooloff and failure threshold in effect for *failures*."""

    tier = get_lockout_tier(failures)
    if tier is not None:
        return EffectiveRule(
            threshold=tier.failures, cooloff=tier.cooloff, is_tier=True
        )

    if settings.AXES_LOCKOUT_TIERS:
        first_tier = sorted(
            settings.AXES_LOCKOUT_TIERS, key=lambda item: item.failures
        )[0]
        return EffectiveRule(
            threshold=first_tier.failures,
            cooloff=first_tier.cooloff,
            is_tier=True,
        )

    return EffectiveRule(
        threshold=get_failure_limit(None, None),
        cooloff=get_cool_off(None),
        is_tier=False,
    )


def classify_attempt(
    attempt: AccessAttempt,
    rule: EffectiveRule,
    now: Optional[timezone.datetime] = None,
) -> str:
    """Classify an attempt as locked, expired, or an ordinary failure."""

    failures = attempt.failures_since_start
    if failures < rule.threshold:
        return STATE_FAILED

    if rule.cooloff is None:
        return STATE_LOCKED

    current_time = now or timezone.now()
    try:
        expires_at = attempt.expiration.expires_at
    except AccessAttemptExpiration.DoesNotExist:
        expires_at = attempt.attempt_time + rule.cooloff

    if expires_at > current_time:
        return STATE_LOCKED
    return STATE_EXPIRED


def collect_lockout_statistics(
    *,
    username: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> list:
    """
    Aggregate access attempts into counts grouped by the effective rule.

    The returned groups never contain usernames, IP addresses, or other
    account identifiers and are ordered so that currently locked tiers are
    reported first.
    """

    attempts = AccessAttempt.objects.all()
    if username is not None:
        attempts = attempts.filter(username=username)
    if ip_address is not None:
        attempts = attempts.filter(ip_address=ip_address)

    current_time = timezone.now()
    groups = {}

    for attempt in attempts.iterator():
        rule = resolve_effective_rule(attempt.failures_since_start)
        group = groups.setdefault(rule.key, LockoutStatisticGroup(rule))
        group.counts[classify_attempt(attempt, rule, current_time)] += 1

    state_order = {state: index for index, state in enumerate(STATISTIC_STATES)}

    def sort_key(group):
        primary_state = next(
            state for state in STATISTIC_STATES if group.counts[state]
        )
        return state_order[primary_state], group.rule.threshold

    return sorted(groups.values(), key=sort_key)


def summarize_statistics(groups: list) -> dict:
    """Return the total count per state across every rule group."""

    totals = {state: 0 for state in STATISTIC_STATES}
    for group in groups:
        for state in STATISTIC_STATES:
            totals[state] += group.counts[state]
    return totals
