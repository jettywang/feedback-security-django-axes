from asgiref.sync import async_to_sync, sync_to_async
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpRequest, HttpResponseForbidden
from django.test import override_settings
from django.urls import path

from axes.backends import AxesStandaloneBackend
from axes.exceptions import AxesBackendPermissionDenied
from axes.middleware import AxesMiddleware
from tests.base import AxesTestCase


def get_username(request, credentials: dict) -> str:
    return credentials.get(settings.AXES_USERNAME_FORM_FIELD)


LOCKOUT_CALLABLE_ARGUMENTS: list = []


def record_lockout_response(request, original_response, credentials):
    LOCKOUT_CALLABLE_ARGUMENTS.append((original_response, credentials))
    return HttpResponse(status=429)


class MiddlewareTestCase(AxesTestCase):
    STATUS_SUCCESS = 200
    STATUS_LOCKOUT = 429

    def setUp(self):
        self.request = HttpRequest()

    def test_success_response(self):
        def get_response(request):
            request.axes_locked_out = False
            return HttpResponse()

        response = AxesMiddleware(get_response)(self.request)
        self.assertEqual(response.status_code, self.STATUS_SUCCESS)

    def test_lockout_response(self):
        def get_response(request):
            request.axes_locked_out = True
            return HttpResponse()

        response = AxesMiddleware(get_response)(self.request)
        self.assertEqual(response.status_code, self.STATUS_LOCKOUT)

    @override_settings(AXES_USERNAME_CALLABLE="tests.test_middleware.get_username")
    def test_lockout_response_with_axes_callable_username(self):
        def get_response(request):
            request.axes_locked_out = True
            request.axes_credentials = {settings.AXES_USERNAME_FORM_FIELD: 'username'}

            return HttpResponse()

        response = AxesMiddleware(get_response)(self.request)
        self.assertEqual(response.status_code, self.STATUS_LOCKOUT)

    @override_settings(AXES_ENABLED=False)
    def test_respects_enabled_switch(self):
        def get_response(request):
            request.axes_locked_out = True
            return HttpResponse()

        response = AxesMiddleware(get_response)(self.request)
        self.assertEqual(response.status_code, self.STATUS_SUCCESS)

    def test_async_lockout_response(self):
        async def get_response(request):
            request.axes_locked_out = True
            return HttpResponse()

        response = async_to_sync(AxesMiddleware(get_response))(self.request)
        self.assertEqual(response.status_code, self.STATUS_LOCKOUT)

    @override_settings(AXES_USERNAME_CALLABLE="tests.test_middleware.get_username")
    def test_async_lockout_response_with_axes_callable_username(self):
        async def get_response(request):
            request.axes_locked_out = True
            request.axes_credentials = {settings.AXES_USERNAME_FORM_FIELD: "username"}

            return HttpResponse()

        response = async_to_sync(AxesMiddleware(get_response))(self.request)
        self.assertEqual(response.status_code, self.STATUS_LOCKOUT)

    @override_settings(
        AXES_LOCKOUT_CALLABLE="tests.test_middleware.record_lockout_response"
    )
    def test_async_lockout_callable_gets_original_response_and_credentials(self):
        LOCKOUT_CALLABLE_ARGUMENTS.clear()
        credentials = {settings.AXES_USERNAME_FORM_FIELD: "username"}
        original_response = HttpResponse()

        async def get_response(request):
            request.axes_locked_out = True
            request.axes_credentials = credentials

            return original_response

        async_to_sync(AxesMiddleware(get_response))(self.request)
        self.assertEqual(LOCKOUT_CALLABLE_ARGUMENTS, [(original_response, credentials)])

    def test_view_exception_response_not_rewritten(self):
        def get_response(request):
            request.axes_locked_out = True
            return HttpResponseForbidden()

        middleware = AxesMiddleware(get_response)
        self.assertIsNone(
            middleware.process_exception(self.request, PermissionDenied("view error"))
        )
        response = middleware(self.request)
        self.assertEqual(response.status_code, 403)

    def test_async_view_exception_response_not_rewritten(self):
        async def get_response(request):
            request.axes_locked_out = True
            return HttpResponseForbidden()

        middleware = AxesMiddleware(get_response)
        middleware.process_exception(self.request, PermissionDenied("view error"))
        response = async_to_sync(middleware)(self.request)
        self.assertEqual(response.status_code, 403)

    def test_lockout_exception_response(self):
        middleware = AxesMiddleware(lambda request: HttpResponse())
        response = middleware.process_exception(
            self.request, AxesBackendPermissionDenied("locked out")
        )
        self.assertEqual(response.status_code, self.STATUS_LOCKOUT)

    def test_lockout_exception_response_not_rewritten_by_call(self):
        lockout_response = HttpResponse(status=self.STATUS_LOCKOUT)

        def get_response(request):
            request.axes_locked_out = True
            return lockout_response

        middleware = AxesMiddleware(get_response)
        middleware.process_exception(
            self.request, AxesBackendPermissionDenied("locked out")
        )
        response = middleware(self.request)
        self.assertIs(response, lockout_response)

    @override_settings(AXES_ENABLED=False)
    def test_process_exception_respects_enabled_switch(self):
        middleware = AxesMiddleware(lambda request: HttpResponse())
        self.assertIsNone(
            middleware.process_exception(
                self.request, AxesBackendPermissionDenied("locked out")
            )
        )


def view_permission_denied(request):
    raise PermissionDenied("View validation failed")


def view_authenticate_then_permission_denied(request):
    authenticate(request, username="axes-view-user", password="axes-view-password")
    raise PermissionDenied("View validation failed")


def view_backend_lockout(request):
    AxesStandaloneBackend().authenticate(
        request, username="axes-view-user", password="axes-view-password"
    )
    return HttpResponse("OK")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("denied/", view_permission_denied),
    path("authenticate-then-denied/", view_authenticate_then_permission_denied),
    path("backend-lockout/", view_backend_lockout),
]


@override_settings(ROOT_URLCONF="tests.test_middleware", AXES_FAILURE_LIMIT=1)
class MiddlewareViewResponseTestCase(AxesTestCase):
    """
    Compare a real lockout request with a plain view failure through the
    full middleware stack, in both sync and async modes, and check that the
    two response types and their log reasons stay distinguishable.
    """

    STATUS_FORBIDDEN = 403

    def post_view(self, client, url):
        return client.post(
            url,
            REMOTE_ADDR=self.ip_address,
            headers={"user-agent": self.user_agent},
        )

    def assert_lockout_response(self, response):
        self.assertEqual(response.status_code, self.BLOCKED)
        self.assertIn(self.LOCKED_MESSAGE, response.content.decode())

    def assert_django_forbidden_response(self, response):
        self.assertEqual(response.status_code, self.STATUS_FORBIDDEN)
        self.assertNotIn(self.LOCKED_MESSAGE, response.content.decode())

    def test_real_lockout_renders_unified_response(self):
        with self.assertLogs("axes", level="WARNING") as logs:
            response = self.lockout()
        self.assert_lockout_response(response)
        self.assertTrue(
            any("AXES: Locking out" in output for output in logs.output),
            logs.output,
        )

    async def test_async_real_lockout_renders_unified_response(self):
        with self.assertLogs("axes", level="WARNING") as logs:
            response = await self.async_client.post(
                self.path_info,
                {"username": self.INVALID_USERNAME, "password": self.INVALID_PASSWORD},
                REMOTE_ADDR=self.ip_address,
                headers={"user-agent": self.user_agent},
            )
        self.assert_lockout_response(response)
        self.assertTrue(
            any("AXES: Locking out" in output for output in logs.output),
            logs.output,
        )

    def test_view_failure_uses_django_handling(self):
        with self.assertLogs("django.request", level="WARNING") as logs:
            response = self.post_view(self.client, "/denied/")
        self.assert_django_forbidden_response(response)
        self.assertTrue(
            any("Forbidden (Permission denied)" in output for output in logs.output),
            logs.output,
        )

    async def test_async_view_failure_uses_django_handling(self):
        with self.assertLogs("django.request", level="WARNING") as logs:
            response = await self.async_client.post(
                "/denied/",
                REMOTE_ADDR=self.ip_address,
                headers={"user-agent": self.user_agent},
            )
        self.assert_django_forbidden_response(response)
        self.assertTrue(
            any("Forbidden (Permission denied)" in output for output in logs.output),
            logs.output,
        )

    def test_locked_request_view_failure_keeps_django_response(self):
        # The view locks the request out via authenticate() and then raises
        # its own PermissionDenied: Django's 403 handling must win over the
        # lockout page.
        with self.assertLogs("django.request", level="WARNING"):
            response = self.post_view(self.client, "/authenticate-then-denied/")
        self.assert_django_forbidden_response(response)

    async def test_async_locked_request_view_failure_keeps_django_response(self):
        with self.assertLogs("django.request", level="WARNING"):
            response = await self.async_client.post(
                "/authenticate-then-denied/",
                REMOTE_ADDR=self.ip_address,
                headers={"user-agent": self.user_agent},
            )
        self.assert_django_forbidden_response(response)

    def test_escaped_lockout_exception_renders_unified_response(self):
        # Already locked out client; the view calls the Axes backend directly
        # and the lockout exception escapes the view.
        self.create_attempt(failures_since_start=1)
        with self.assertLogs("axes", level="WARNING") as logs:
            response = self.post_view(self.client, "/backend-lockout/")
        self.assert_lockout_response(response)
        self.assertTrue(
            any(
                "AXES: Locked out authentication attempt" in output
                for output in logs.output
            ),
            logs.output,
        )

    async def test_async_escaped_lockout_exception_renders_unified_response(self):
        await sync_to_async(self.create_attempt)(failures_since_start=1)
        with self.assertLogs("axes", level="WARNING") as logs:
            response = await self.async_client.post(
                "/backend-lockout/",
                REMOTE_ADDR=self.ip_address,
                headers={"user-agent": self.user_agent},
            )
        self.assert_lockout_response(response)
        self.assertTrue(
            any(
                "AXES: Locked out authentication attempt" in output
                for output in logs.output
            ),
            logs.output,
        )
