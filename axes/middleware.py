from logging import getLogger
from typing import Callable, Optional

from asgiref.sync import iscoroutinefunction, markcoroutinefunction, sync_to_async
from django.conf import settings
from django.http import HttpRequest, HttpResponse

from axes.exceptions import AxesBackendPermissionDenied
from axes.helpers import get_lockout_response

log = getLogger(__name__)


class AxesMiddleware:
    """
    Middleware that calculates necessary HTTP request attributes for attempt monitoring
    and maps lockout signals into readable HTTP 403 Forbidden responses.

    If a project uses ``django rest framework`` then the middleware updates the
    request and checks whether the limit has been exceeded. It's needed only
    for integration with DRF because it uses its own request object.

    This middleware recognizes a logout monitoring flag in the request and
    and uses the ``axes.helpers.get_lockout_response`` handler for returning
    customizable and context aware lockout message to the end user if necessary.

    Exceptions raised by the view itself are left to Django's own exception
    handling. Only lockouts signalled by Axes, either via the
    ``request.axes_locked_out`` flag or via an ``AxesBackendPermissionDenied``
    exception escaping the authentication flow, produce the lockout response.

    To customize the lockout handling behaviour further, you can subclass this middleware
    and change the ``__call__`` method to your own liking.

    Please see the following configuration flags before customizing this handler:

    - ``AXES_LOCKOUT_TEMPLATE``,
    - ``AXES_LOCKOUT_URL``,
    - ``AXES_COOLOFF_MESSAGE``, and
    - ``AXES_PERMALOCK_MESSAGE``.
    """

    async_capable = True
    sync_capable = True

    def __init__(self, get_response: Callable) -> None:
        self.get_response = get_response
        if iscoroutinefunction(self.get_response):
            markcoroutinefunction(self)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Exit out to async mode, if needed
        if iscoroutinefunction(self):
            return self.__acall__(request)

        response = self.get_response(request)
        if settings.AXES_ENABLED and not getattr(request, "axes_view_exception", False):
            if getattr(request, "axes_locked_out", None):
                credentials = getattr(request, "axes_credentials", None)
                response = get_lockout_response(request, response, credentials)  # type: ignore

        return response

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        response = await self.get_response(request)

        if settings.AXES_ENABLED and not getattr(request, "axes_view_exception", False):
            if getattr(request, "axes_locked_out", None):
                credentials = getattr(request, "axes_credentials", None)
                response = await sync_to_async(
                    get_lockout_response, thread_sensitive=True
                )(
                    request, response, credentials
                )  # type: ignore

        return response

    def process_exception(
        self, request: HttpRequest, exception: Exception
    ) -> Optional[HttpResponse]:
        """
        Keep lockouts and view errors on separate response paths.

        An ``AxesBackendPermissionDenied`` escaping the view means a locked
        out authentication attempt, e.g. from a view calling the Axes backend
        directly. Answer it with the unified lockout response instead of the
        generic 403 Django renders for plain ``PermissionDenied`` exceptions.

        Any other exception belongs to the view itself. Mark the request so
        the lockout flag does not rewrite Django's own error response, and
        let Django's exception handling produce the response.
        """

        if not settings.AXES_ENABLED:
            return None

        # The response for this request is owned by exception handling;
        # __call__ must not rewrite it based on the lockout flag.
        request.axes_view_exception = True  # type: ignore[attr-defined]

        if isinstance(exception, AxesBackendPermissionDenied):
            credentials = getattr(request, "axes_credentials", None)
            log.warning(
                "AXES: Locked out authentication attempt to %s raised %s, "
                "responding with the configured lockout response.",
                request.path,
                exception.__class__.__name__,
            )
            return get_lockout_response(request, credentials=credentials)

        return None
