"""
Contract tests for the shared failure-event boundary in ``AxesBaseHandler``.

Failure events reach handlers through two paths:

1. directly from ``django.contrib.auth.signals.user_login_failed`` and
2. forwarded by ``AxesProxyHandler.user_login_failed``.

Both paths must hand the same authentication context (request, credentials)
and lockout state (``axes_locked_out``, ``axes_credentials``) to the handler.
These tests pin that contract with a recording fake handler and cross-check
the same three scenarios against the shipped database and cache handlers.
"""

from logging import getLogger
from unittest.mock import patch

from django.contrib.auth.signals import user_login_failed
from django.test import override_settings

from axes.handlers.base import AxesBaseHandler, AbstractAxesHandler
from axes.handlers.proxy import AxesProxyHandler
from axes.signals import user_locked_out
from tests.base import AxesTestCase

log = getLogger(__name__)


def record_lockout(sender, **kwargs):
    record_lockout.calls.append(kwargs)


record_lockout.calls = []


class RecordingAxesHandler(AbstractAxesHandler, AxesBaseHandler):
    """
    Custom handler that keeps the stock ``AXES_HANDLER`` wiring but only
    records the arguments and request state seen at the failure boundary.
    """

    calls = []

    def user_login_failed(self, sender, credentials, request=None, **kwargs):
        if not self.has_request(request, log):
            return

        already_locked = self.is_lockout_already_active(request, credentials)
        if already_locked:
            # Mirrors the shipped handlers: no new failure is recorded while
            # the shared lockout boundary has already handled the request.
            return

        self.calls.append(
            {
                "sender": sender,
                "credentials": credentials,
                "request": request,
                "already_locked": already_locked,
                "axes_locked_out": request.axes_locked_out,
                "axes_credentials": getattr(request, "axes_credentials", None),
            }
        )

    def user_logged_in(self, sender, request, user, **kwargs):
        pass

    def user_logged_out(self, sender, request, user, **kwargs):
        pass

    def get_failures(self, request, credentials=None):
        return 0


class FailureContractMixin:
    """Three shared scenarios run against every handler under test."""

    def test_missing_request_is_ignored(self):
        # Direct signal path: Django is allowed to emit without a request.
        user_login_failed.send(
            sender=None, credentials=self.credentials, request=None
        )
        # Forwarded path: the proxy must also survive a missing request.
        AxesProxyHandler.user_login_failed(
            sender=None, credentials=self.credentials, request=None
        )

        self.assertFalse(getattr(self.request, "axes_locked_out", False))
        self._assert_no_failure_recorded()

    def test_failure_while_already_locked_reuses_lockout_state(self):
        record_lockout.calls = []
        user_locked_out.connect(record_lockout)
        try:
            self.request.axes_locked_out = True

            with override_settings(
                AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT=False
            ):
                user_login_failed.send(
                    sender=None,
                    credentials=self.credentials,
                    request=self.request,
                )

            self.assertTrue(self.request.axes_locked_out)
            self.assertEqual(self.credentials, self.request.axes_credentials)
            self.assertEqual(1, len(record_lockout.calls))
            self.assertEqual(
                self.request.axes_ip_address,
                record_lockout.calls[0]["ip_address"],
            )
            self.assertEqual(self.username, record_lockout.calls[0]["username"])
            self._assert_no_failure_recorded()
        finally:
            user_locked_out.disconnect(record_lockout)

    def test_first_failure_records_attempt_without_locking(self):
        record_lockout.calls = []
        user_locked_out.connect(record_lockout)
        try:
            # Exceeding the limit is checked from stored failures, so the very
            # first failure must never lock or emit the lockout signal.
            user_login_failed.send(
                sender=None,
                credentials=self.credentials,
                request=self.request,
            )

            self.assertFalse(self.request.axes_locked_out)
            self.assertEqual(0, len(record_lockout.calls))
            self._assert_single_failure_recorded()
        finally:
            user_locked_out.disconnect(record_lockout)

    def _assert_no_failure_recorded(self):
        raise NotImplementedError

    def _assert_single_failure_recorded(self):
        raise NotImplementedError


@override_settings(
    AXES_HANDLER="tests.test_failure_contract.RecordingAxesHandler"
)
class RecordingHandlerFailureContractTestCase(FailureContractMixin, AxesTestCase):
    def setUp(self):
        super().setUp()
        RecordingAxesHandler.calls = []

    def test_custom_handler_wiring_is_preserved(self):
        self.assertIsInstance(
            AxesProxyHandler.get_implementation(force=True), RecordingAxesHandler
        )

    def test_recording_handler_receives_authentication_context(self):
        sender = object()
        user_login_failed.send(
            sender=sender,
            credentials=self.credentials,
            request=self.request,
        )

        self.assertEqual(1, len(RecordingAxesHandler.calls))
        recorded = RecordingAxesHandler.calls[0]
        self.assertIs(sender, recorded["sender"])
        self.assertEqual(self.credentials, recorded["credentials"])
        self.assertIs(self.request, recorded["request"])
        self.assertFalse(recorded["already_locked"])
        self.assertFalse(recorded["axes_locked_out"])

    @patch("tests.test_failure_contract.log")
    def test_missing_request_logged_through_shared_boundary(self, log):
        AxesProxyHandler.user_login_failed(
            sender=None, credentials={}, request=None
        )
        log.error.assert_called_with(
            "AXES: RecordingAxesHandler.user_login_failed "
            "does not function without a request."
        )
        self.assertEqual(0, len(RecordingAxesHandler.calls))

    def _assert_no_failure_recorded(self):
        self.assertEqual(0, len(RecordingAxesHandler.calls))

    def _assert_single_failure_recorded(self):
        self.assertEqual(1, len(RecordingAxesHandler.calls))


@override_settings(
    AXES_HANDLER="axes.handlers.database.AxesDatabaseHandler",
    AXES_ENABLE_ACCESS_FAILURE_LOG=True,
)
class DatabaseHandlerFailureContractTestCase(FailureContractMixin, AxesTestCase):
    def _assert_no_failure_recorded(self):
        from axes.models import AccessAttempt, AccessFailureLog

        self.assertEqual(0, AccessAttempt.objects.count())
        self.assertEqual(0, AccessFailureLog.objects.count())

    def _assert_single_failure_recorded(self):
        from axes.models import AccessAttempt, AccessFailureLog

        self.assertEqual(1, AccessAttempt.objects.count())
        self.assertEqual(1, AccessAttempt.objects.get().failures_since_start)
        self.assertEqual(1, AccessFailureLog.objects.count())


@override_settings(AXES_HANDLER="axes.handlers.cache.AxesCacheHandler")
class CacheHandlerFailureContractTestCase(FailureContractMixin, AxesTestCase):
    def _assert_no_failure_recorded(self):
        self.assertEqual(
            0, AxesProxyHandler.get_failures(self.request, self.credentials)
        )

    def _assert_single_failure_recorded(self):
        self.assertEqual(
            1, AxesProxyHandler.get_failures(self.request, self.credentials)
        )
        self.assertEqual(1, self.request.axes_failures_since_start)
