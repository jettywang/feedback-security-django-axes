"""
Contract tests shared by the database and cache Axes handlers.

Both storage backends are driven through the exact same sequence of
authentication events and the exact same expected-behaviour table.  The only
things that differ between the two concrete test cases are:

* the value of ``settings.AXES_HANDLER`` (and, for the cache handler, a cache
  backend whose clock can be controlled), and
* the adapter level view used to inspect the raw storage state.

The time used for every decision is taken from a single controllable clock.
The database handler reads it through ``request.axes_attempt_time`` while the
cache handler reads it through the test cache backend, so neither test has to
sleep for a cool off to elapse.

Each row of the expected table is a :class:`ContractEvent`.  The events are
replayed inside labelled ``subTest`` blocks so a contract violation is reported
at the position (the authentication event) where the behaviour first diverges.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache.backends.locmem import LocMemCache
from django.http import HttpRequest
from django.test import TestCase, override_settings

from axes.conf import settings
from axes.handlers.proxy import AxesProxyHandler
from axes.helpers import get_client_http_accept, get_client_ip_address
from axes.helpers import get_client_path_info, get_client_user_agent
from axes.models import AccessAttempt

CONTRACT_IP_ADDRESS = "10.9.8.7"
CONTRACT_USERNAME = "contract-user"
CONTRACT_PASSWORD = "contract-password"
CONTRACT_USER_AGENT = "contract-user-agent"
CONTRACT_PATH = "/admin/login/"

# Fixed epoch keeps every run deterministic and independent of the wall clock.
CONTRACT_EPOCH = datetime(2026, 1, 1, 0, 0, 0)
COOLOFF = timedelta(minutes=30)
FAILURE_LIMIT = 3


class FakeClock:
    """Controllable monotonic clock expressed in POSIX seconds."""

    def __init__(self, epoch: datetime):
        self.current = epoch

    def now(self) -> datetime:
        return self.current

    def timestamp(self) -> float:
        return self.current.timestamp()

    def advance(self, delta: timedelta) -> None:
        self.current += delta


class FakeClockLocMemCache(LocMemCache):
    """LocMem cache that measures expiry against :class:`FakeClock`."""

    clock: Optional[FakeClock] = None

    def get_backend_timeout(self, timeout=None):
        if timeout is None:
            timeout = self.default_timeout
        if timeout == 0:
            return None
        return self.clock.timestamp() + timeout

    def _has_expired(self, key):
        expiration = self._expire_info.get(key, -1)
        return expiration is not None and expiration <= self.clock.timestamp()


@dataclass
class ContractEvent:
    """One authentication event and the behaviour promised by both handlers.

    ``advance`` moves the controllable clock before the event is dispatched.
    ``locked_out`` and ``failures`` describe the shared contract observable
    through the handler API right after the event.  ``stale_attempt_rows`` is
    not part of the contract: it records how many raw ``AccessAttempt`` rows
    the database adapter still keeps after the event, preserving the storage
    difference where expired rows linger until the next database cleanup while
    cache entries simply expire away.
    """

    name: str
    dispatch: Callable[["HandlerContractTestCase"], None]
    advance: timedelta = timedelta(0)
    locked_out: bool = False
    failures: int = 0
    stale_attempt_rows: int = 0


def _dispatch_failure(case: "HandlerContractTestCase") -> None:
    request = case.new_request()
    AxesProxyHandler.user_login_failed(
        sender=None,
        request=request,
        credentials={"username": CONTRACT_USERNAME, "password": CONTRACT_PASSWORD},
    )


def _dispatch_success(case: "HandlerContractTestCase") -> None:
    request = case.new_request()
    AxesProxyHandler.user_logged_in(
        sender=None, request=request, user=case.user
    )


def _dispatch_reset(case: "HandlerContractTestCase") -> None:
    AxesProxyHandler.reset_attempts(ip_address=CONTRACT_IP_ADDRESS)


# One table, replayed unchanged against every handler implementation.
CONTRACT_EVENTS: List[ContractEvent] = [
    ContractEvent(
        name="01 first failure",
        dispatch=_dispatch_failure,
        failures=1,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="02 second failure",
        dispatch=_dispatch_failure,
        failures=2,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="03 third failure locks out",
        dispatch=_dispatch_failure,
        locked_out=True,
        failures=3,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="04 still locked within cool off",
        dispatch=_dispatch_failure,
        advance=timedelta(seconds=1),
        locked_out=True,
        failures=4,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="05 cool off expires and count restarts",
        dispatch=_dispatch_failure,
        advance=COOLOFF + timedelta(seconds=1),
        failures=1,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="06 one more failure",
        dispatch=_dispatch_failure,
        failures=2,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="07 successful login resets failures",
        dispatch=_dispatch_success,
        failures=0,
        stale_attempt_rows=0,
    ),
    ContractEvent(
        name="08 failure after success starts fresh",
        dispatch=_dispatch_failure,
        failures=1,
        stale_attempt_rows=1,
    ),
    ContractEvent(
        name="09 explicit reset clears storage",
        dispatch=_dispatch_reset,
        failures=0,
        stale_attempt_rows=0,
    ),
]


class HandlerContractTestCase(TestCase):
    """Replay :data:`CONTRACT_EVENTS` against a configured handler.

    Concrete subclasses only supply the handler path, any extra settings the
    storage backend needs (such as the controllable cache) and a raw storage
    observation.  The base class itself is excluded from test collection.
    """

    handler_path: str = ""
    extra_settings: dict = {}

    def setUp(self):
        self.clock = FakeClock(CONTRACT_EPOCH)
        FakeClockLocMemCache.clock = self.clock
        AxesProxyHandler.implementation = None

        self.user = get_user_model().objects.create_user(
            username=CONTRACT_USERNAME, password=CONTRACT_PASSWORD
        )

    def tearDown(self):
        AxesProxyHandler.implementation = None
        FakeClockLocMemCache.clock = None
        for cache in self._axes_caches():
            cache.clear()

    def _axes_caches(self):
        from django.core.cache import caches

        names = ["default"]
        if "axes" in caches.settings:
            names.append("axes")
        return [caches[name] for name in names]

    def new_request(self) -> HttpRequest:
        request = HttpRequest()
        request.method = "POST"
        request.META["REMOTE_ADDR"] = CONTRACT_IP_ADDRESS
        request.META["HTTP_USER_AGENT"] = CONTRACT_USER_AGENT
        request.META["PATH_INFO"] = CONTRACT_PATH

        # The database handler bases every cool off decision on this value.
        request.axes_attempt_time = self.clock.now()
        request.axes_ip_address = get_client_ip_address(request)
        request.axes_user_agent = get_client_user_agent(request)
        request.axes_path_info = get_client_path_info(request)
        request.axes_http_accept = get_client_http_accept(request)
        request.axes_failures_since_start = None
        request.axes_locked_out = False
        return request

    def observe_storage(self, request: HttpRequest) -> dict:
        """Adapter specific view of the raw storage state.

        Subclasses override this while keeping the shared contract assertions
        untouched, which is where the observed state differences live.
        """
        raise NotImplementedError

    def assert_adapter_storage(
        self, event: ContractEvent, observation: dict
    ) -> None:
        """Adapter specific assertions about raw, non contract state."""
        raise NotImplementedError

    def test_authentication_event_contract(self):
        if not self.handler_path:
            self.skipTest("Abstract contract table; run via a concrete handler.")
        contract_settings = {
            "AXES_FAILURE_LIMIT": FAILURE_LIMIT,
            "AXES_COOLOFF_TIME": COOLOFF,
            "AXES_RESET_ON_SUCCESS": True,
            "AXES_LOCK_OUT_AT_FAILURE": True,
        }
        contract_settings.update(self.extra_settings)

        # Route every handler decision, including the proxy's own request
        # stamping, through the controllable clock instead of the wall clock.
        original_update_request = AxesProxyHandler.update_request

        def timed_update_request(request):
            original_update_request(request)
            if request is not None:
                request.axes_attempt_time = self.clock.now()

        with override_settings(**contract_settings):
            with override_settings(AXES_HANDLER=self.handler_path):
                # The handler path is switched last so the axes setting-changed
                # receiver builds the implementation after the cache connection
                # it depends on already exists.
                AxesProxyHandler.implementation = None
                with patch.object(
                    AxesProxyHandler,
                    "update_request",
                    staticmethod(timed_update_request),
                ):
                    self._replay_contract()

    def _replay_contract(self):
        for event in CONTRACT_EVENTS:
            with self.subTest(event=event.name):
                self.clock.advance(event.advance)
                event.dispatch(self)

                request = self.new_request()
                self.assertEqual(
                    event.locked_out,
                    AxesProxyHandler.is_locked(
                        request,
                        {"username": CONTRACT_USERNAME},
                    ),
                )
                self.assertEqual(
                    event.failures,
                    AxesProxyHandler.get_failures(
                        request,
                        {"username": CONTRACT_USERNAME},
                    ),
                )
                self.assertEqual(
                    event.failures < settings.AXES_FAILURE_LIMIT,
                    AxesProxyHandler.is_allowed(
                        request,
                        {"username": CONTRACT_USERNAME},
                    ),
                )

                observation = self.observe_storage(request)
                self.assertEqual(
                    event.failures, observation["live_failures"]
                )
                self.assert_adapter_storage(event, observation)


class DatabaseHandlerContractTestCase(HandlerContractTestCase):
    handler_path = "axes.handlers.database.AxesDatabaseHandler"

    def observe_storage(self, request):
        attempts = list(
            AccessAttempt.objects.filter(ip_address=CONTRACT_IP_ADDRESS)
        )
        live_failures = sum(
            attempt.failures_since_start
            for attempt in attempts
            if attempt.attempt_time
            >= self.clock.now() - settings.AXES_COOLOFF_TIME
        )
        return {
            "live_failures": live_failures,
            "stored_rows": len(attempts),
        }

    def assert_adapter_storage(self, event, observation):
        # Expired AccessAttempt rows are kept until the next handler call
        # cleans them up instead of disappearing the moment cool off elapses.
        self.assertEqual(event.stale_attempt_rows, observation["stored_rows"])


CONTRACT_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "axes": {
        "BACKEND": "tests.test_handler_contract.FakeClockLocMemCache",
    },
}


class CacheHandlerContractTestCase(HandlerContractTestCase):
    handler_path = "axes.handlers.cache.AxesCacheHandler"
    extra_settings = {
        "AXES_CACHE": "axes",
        "CACHES": CONTRACT_CACHES,
    }

    def observe_storage(self, request):
        from axes.helpers import get_client_cache_keys

        cache = AxesProxyHandler.get_implementation().cache
        cache_keys = get_client_cache_keys(
            request, {"username": CONTRACT_USERNAME}
        )
        stored_keys = [key for key in cache_keys if cache.get(key) is not None]
        return {
            "live_failures": max(
                (cache.get(key, default=0) for key in cache_keys),
                default=0,
            ),
            "stored_keys": len(stored_keys),
        }

    def assert_adapter_storage(self, event, observation):
        # Cache entries vanish on expiry: a zero failure count means no live
        # entry remains, which is the cache counterpart of the database's
        # lingering expired rows.
        expected_keys = 0 if event.failures == 0 else 1
        self.assertEqual(expected_keys, observation["stored_keys"])
