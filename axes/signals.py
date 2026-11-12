from logging import getLogger

from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.core.signals import setting_changed
from django.db.models.signals import post_save, post_delete
from django.dispatch import Signal
from django.dispatch import receiver

from axes.handlers.proxy import AxesProxyHandler
from axes.models import AccessAttempt

log = getLogger(__name__)


# This signal provides the following arguments to any listeners:
# request - The current Request object.
# username - The username of the User who has been locked out.
# ip_address - The IP of the user who has been locked out.
user_locked_out = Signal()

# Audit signal emitted after an explicit unlock action (a reset or deletion of
# AccessAttempt records) has completed successfully.  It deliberately carries
# no usernames, IP addresses, or other raw client identifiers.
#
# Listeners receive:
# entry_point - Where the unlock was initiated: ``"admin"``, ``"command"``,
#               or ``"api"``.
# scope       - What was selected for deletion: ``"all"``, ``"ip"``,
#               ``"username"``, ``"ip_and_username"``, ``"ip_or_username"``,
#               ``"instance"``, or ``"selection"``.
# status      - ``"success"`` when every requested record was removed,
#               ``"partial"`` when some but not all requested records were
#               removed, and ``"noop"`` when no records were removed.
# requested   - Number of AccessAttempt records the action intended to remove.
# affected    - Number of AccessAttempt records actually removed.
user_unlocked = Signal()

# Descriptive alias for listeners that subscribe to reset events rather than
# user unlock events.  Both names refer to the same signal.
access_attempts_reset = user_unlocked


def emit_unlock_event(
    *,
    entry_point: str,
    scope: str,
    requested: int,
    affected: int,
) -> None:
    """
    Send a single ``user_unlocked`` audit event for a completed unlock action.

    The event is sent only by callers that have finished the deletion (or
    established that there was nothing to delete).  Callers must not send this
    signal when the backend operation raised; otherwise a failed reset would
    leave a success audit trail behind.

    The ``status`` field distinguishes an empty run (``"noop"``) from a run
    that removed only a subset of what was requested (``"partial"``).  A
    successful full reset is ``"success"``.

    No usernames, IP addresses, request data, or exceptions are included in
    the event.
    """

    if affected < 0 or requested < 0:
        raise ValueError("requested and affected must be non-negative")
    if affected > requested:
        raise ValueError("affected can not exceed requested")

    if affected == 0:
        status = "noop"
    elif affected < requested:
        status = "partial"
    else:
        status = "success"

    # send_robust keeps a failing audit listener from turning a completed
    # unlock into an exception (and, inside the admin transaction, from
    # rolling back data that was already deleted).
    user_unlocked.send_robust(
        sender="axes",
        entry_point=entry_point,
        scope=scope,
        status=status,
        requested=requested,
        affected=affected,
    )


@receiver(user_login_failed)
def handle_user_login_failed(*args, **kwargs):
    AxesProxyHandler.user_login_failed(*args, **kwargs)


@receiver(user_logged_in)
def handle_user_logged_in(*args, **kwargs):
    AxesProxyHandler.user_logged_in(*args, **kwargs)


@receiver(user_logged_out)
def handle_user_logged_out(*args, **kwargs):
    AxesProxyHandler.user_logged_out(*args, **kwargs)


@receiver(post_save, sender=AccessAttempt)
def handle_post_save_access_attempt(*args, **kwargs):
    AxesProxyHandler.post_save_access_attempt(*args, **kwargs)


@receiver(post_delete, sender=AccessAttempt)
def handle_post_delete_access_attempt(*args, **kwargs):
    AxesProxyHandler.post_delete_access_attempt(*args, **kwargs)


@receiver(setting_changed)
def handle_setting_changed(
    sender, setting, value, enter, **kwargs
):  # pylint: disable=unused-argument
    """
    Reinitialize handler implementation if a relevant setting changes
    in e.g. application reconfiguration or during testing.
    """

    if setting == "AXES_HANDLER":
        AxesProxyHandler.get_implementation(force=True)
