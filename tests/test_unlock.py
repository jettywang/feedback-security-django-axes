from unittest.mock import MagicMock, patch

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

import axes.admin  # noqa: F401  (registers the admin model admins)
from axes.admin import AccessAttemptAdmin
from axes.models import AccessAttempt
from axes.signals import user_unlocked
from axes.unlock import delete_access_attempts
from tests.base import AxesTestCase


class UnlockAuditTestCase(AxesTestCase):
    """
    Verify that the Axes unlock audit event is emitted consistently from the
    Django admin and management command entry points.
    """

    ENTRY_KEYS = {"entry_point", "scope", "status", "requested", "affected"}

    def setUp(self):
        super().setUp()
        self.events = []
        user_unlocked.connect(
            self._record_event, weak=False, dispatch_uid=id(self)
        )
        self.addCleanup(user_unlocked.disconnect, dispatch_uid=id(self))

        if not django_admin.site.is_registered(AccessAttempt):
            django_admin.site.register(AccessAttempt, AccessAttemptAdmin)
        self.admin = django_admin.site._registry[AccessAttempt]

    def _record_event(self, sender, signal=None, **kwargs):
        self.events.append(kwargs)

    def create_attempts(self, count=3):
        attempts = []
        for index in range(count):
            attempts.append(
                self.create_attempt(
                    username=f"audit-user-{index}",
                    ip_address=f"203.0.113.{index + 1}",
                    user_agent=f"audit-agent-{index}",
                )
            )
        return attempts

    def assert_single_event(self, **expected):
        self.assertEqual(1, len(self.events))
        event = self.events[0]
        self.assertEqual(self.ENTRY_KEYS, set(event))
        for key, value in expected.items():
            self.assertEqual(value, event[key], key)
        return event

    def get_admin_client(self):
        client = self.client_class()
        client.force_login(
            get_user_model().objects.create_superuser(
                username="audit-admin",
                password=self.password,
                email="audit-admin@example.com",
            )
        )
        return client

    def admin_delete_selected(self, attempts, client):
        return client.post(
            reverse("admin:axes_accessattempt_changelist"),
            {
                "action": "delete_selected",
                "post": "yes",
                "_selected_action": [str(attempt.pk) for attempt in attempts],
            },
        )

    def test_command_reset_reports_success(self):
        self.create_attempts(3)

        call_command("axes_reset")

        self.assert_single_event(
            entry_point="command",
            scope="all",
            status="success",
            requested=3,
            affected=3,
        )
        self.assertFalse(AccessAttempt.objects.exists())

    def test_command_reset_noop_is_distinguishable(self):
        call_command("axes_reset")

        self.assert_single_event(
            entry_point="command",
            scope="all",
            status="noop",
            requested=0,
            affected=0,
        )

    def test_command_reset_missing_ip_is_noop(self):
        self.create_attempts(2)

        call_command("axes_reset_ip", "198.51.100.7")

        self.assert_single_event(
            entry_point="command",
            scope="ip",
            status="noop",
            requested=0,
            affected=0,
        )
        self.assertEqual(2, AccessAttempt.objects.count())

    def test_command_reset_username_reports_affected(self):
        self.create_attempt(username="audit-target", failures_since_start=2)
        self.create_attempt(username="audit-other", failures_since_start=1)

        call_command("axes_reset_username", "audit-target")

        self.assert_single_event(
            entry_point="command",
            scope="username",
            status="success",
            requested=1,
            affected=1,
        )
        self.assertEqual(
            ["audit-other"],
            list(AccessAttempt.objects.values_list("username", flat=True)),
        )

    def test_admin_single_delete_reports_success(self):
        attempt = self.create_attempts(1)[0]

        self.admin.delete_model(self.request, attempt)

        self.assert_single_event(
            entry_point="admin",
            scope="instance",
            status="success",
            requested=1,
            affected=1,
        )
        self.assertFalse(AccessAttempt.objects.exists())

    def test_admin_single_delete_of_stale_object_is_noop(self):
        attempt = self.create_attempts(1)[0]
        AccessAttempt.objects.filter(pk=attempt.pk).delete()

        self.admin.delete_model(self.request, attempt)

        self.assert_single_event(
            entry_point="admin",
            scope="instance",
            status="noop",
            requested=1,
            affected=0,
        )

    def test_admin_delete_selected_reports_selection(self):
        attempts = self.create_attempts(3)

        response = self.admin_delete_selected(attempts, self.get_admin_client())

        self.assertEqual(302, response.status_code)
        self.assert_single_event(
            entry_point="admin",
            scope="selection",
            status="success",
            requested=3,
            affected=3,
        )
        self.assertFalse(AccessAttempt.objects.exists())

    def test_admin_delete_selected_of_stale_selection_is_noop(self):
        self.create_attempt(username="keep-user", ip_address="198.51.100.9", failures_since_start=1)
        self.create_attempt(username="delete-user", ip_address="198.51.100.8", failures_since_start=1)
        stale = list(AccessAttempt.objects.filter(username="delete-user"))
        AccessAttempt.objects.filter(pk=stale[0].pk).delete()

        response = self.admin_delete_selected(stale, self.get_admin_client())

        self.assertEqual(302, response.status_code)
        self.assert_single_event(
            entry_point="admin",
            scope="selection",
            status="noop",
            requested=0,
            affected=0,
        )
        self.assertEqual(1, AccessAttempt.objects.count())

    def test_admin_and_command_events_agree_for_full_reset(self):
        self.create_attempts(2)
        call_command("axes_reset")
        command_event = self.events[-1]

        self.events.clear()
        attempts = self.create_attempts(2)
        self.admin.delete_queryset(
            self.request,
            AccessAttempt.objects.filter(pk__in=[attempt.pk for attempt in attempts]),
        )

        admin_event = self.assert_single_event(
            entry_point="admin",
            scope="selection",
            status="success",
            requested=command_event["requested"],
            affected=command_event["affected"],
        )
        self.assertEqual("success", admin_event["status"])

    def test_partial_deletion_is_reported(self):
        queryset = MagicMock()
        queryset.count.return_value = 3
        queryset.delete.return_value = (3, {"axes.AccessAttempt": 1})

        delete_access_attempts(queryset, entry_point="admin", scope="selection")

        self.assert_single_event(
            entry_point="admin",
            scope="selection",
            status="partial",
            requested=3,
            affected=1,
        )

    def test_backend_error_leaves_no_success_audit(self):
        self.create_attempts(2)

        with patch(
            "axes.handlers.database.delete_access_attempts",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                call_command("axes_reset")

        self.assertEqual([], self.events)

    def test_delete_error_leaves_no_success_audit(self):
        queryset = AccessAttempt.objects.all()
        with patch.object(
            queryset,
            "delete",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                delete_access_attempts(queryset, entry_point="admin", scope="selection")

        self.assertEqual([], self.events)

    def test_event_carries_no_raw_identity(self):
        self.create_attempt(
            username="very-secret-user",
            ip_address="203.0.113.42",
            user_agent="secret-agent",
            failures_since_start=1,
        )

        call_command("axes_reset_username", "very-secret-user")

        event = self.assert_single_event(
            entry_point="command",
            scope="username",
            status="success",
            requested=1,
            affected=1,
        )
        rendered = repr(event)
        self.assertNotIn("very-secret-user", rendered)
        self.assertNotIn("203.0.113.42", rendered)
        self.assertNotIn("secret-agent", rendered)

    def test_signal_alias_observes_the_same_event(self):
        from axes.signals import access_attempts_reset

        received = []

        def record_alias(sender, signal=None, **kwargs):
            received.append(kwargs)

        access_attempts_reset.connect(
            record_alias,
            weak=False,
            dispatch_uid="audit-alias",
        )
        self.addCleanup(
            access_attempts_reset.disconnect, dispatch_uid="audit-alias"
        )

        call_command("axes_reset")

        self.assert_single_event(
            entry_point="command",
            scope="all",
            status="noop",
            requested=0,
            affected=0,
        )
        self.assertEqual(self.events, received)
