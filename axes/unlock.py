"""
Helpers for auditing explicit unlock actions.

An unlock is any operation that removes :class:`axes.models.AccessAttempt`
records in order to let a client authenticate again.  The helpers in this
module perform the deletion and emit the ``axes.signals.user_unlocked`` audit
event only after the storage backend has completed the deletion.
"""

from typing import Optional

from django.db.models import QuerySet

from axes.models import AccessAttempt
from axes.signals import emit_unlock_event


def get_unlock_scope(
    *,
    ip_address: Optional[str] = None,
    username: Optional[str] = None,
    ip_or_username: bool = False,
) -> str:
    """
    Return the machine-readable scope for a parameter based attempt reset.
    """

    if ip_or_username:
        return "ip_or_username"
    if ip_address and username:
        return "ip_and_username"
    if ip_address:
        return "ip"
    if username:
        return "username"
    return "all"


def _affected_attempts(deleted_per_model: dict) -> int:
    return deleted_per_model.get(AccessAttempt._meta.label, 0)


def delete_access_attempts(queryset: QuerySet, *, entry_point: str, scope: str) -> int:
    """
    Delete the given ``AccessAttempt`` queryset and audit the outcome.

    ``requested`` is the number of matching attempts before deletion and
    ``affected`` is the number of attempts that were actually removed.  The
    audit event is emitted only after ``delete()`` returns; if the database
    operation raises, no success audit is produced.
    """

    requested = queryset.count()
    _, deleted_per_model = queryset.delete()
    affected = _affected_attempts(deleted_per_model)

    emit_unlock_event(
        entry_point=entry_point,
        scope=scope,
        requested=requested,
        affected=affected,
    )
    return affected


def delete_access_attempt(
    instance: AccessAttempt, *, entry_point: str, scope: str
) -> int:
    """
    Delete a single ``AccessAttempt`` instance and audit the outcome.
    """

    requested = 1
    _, deleted_per_model = instance.delete()
    affected = _affected_attempts(deleted_per_model)

    emit_unlock_event(
        entry_point=entry_point,
        scope=scope,
        requested=requested,
        affected=affected,
    )
    return affected
