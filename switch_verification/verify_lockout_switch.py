"""
Offline comparison of the two Axes lockout-key configurations on identical data:

  OLD (lock by username only):            AXES_LOCKOUT_PARAMETERS = ["username"]
  NEW (username AND client address):      AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]

Everything runs against an in-memory SQLite database and a local LocMem cache.
No network, real account, or running service is involved.

Run:  .venv/bin/python switch_verification/verify_lockout_switch.py
"""

import django
from django.conf import settings

FAILURE_LIMIT = 5

settings.configure(
    DEBUG=True,
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    },
    INSTALLED_APPS=[
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "axes",
    ],
    SECRET_KEY="offline-verification",
    USE_TZ=True,
    AXES_FAILURE_LIMIT=FAILURE_LIMIT,
    # start from the OLD configuration; it is toggled at runtime below
    AXES_LOCKOUT_PARAMETERS=["username"],
)
django.setup()

from django.core.management import call_command
from django.db import transaction
from django.http import HttpRequest
from django.test import override_settings
from django.utils.timezone import now

from axes.helpers import get_client_cache_keys, get_client_parameters
from axes.models import AccessAttempt
from axes.handlers.database import AxesDatabaseHandler

call_command("migrate", run_syncdb=True, verbosity=0)

OLD = ["username"]
NEW = [["username", "ip_address"]]
UA_CHROME, UA_FIREFOX, UA_SAFARI = "Chrome", "Firefox", "Safari"

# ---------------------------------------------------------------------------
# Synthetic AccessAttempt rows. The PHYSICAL write key is always
# (username, ip_address, user_agent) -- see AccessAttempt.Meta.unique_together
# and the get_or_create(...) call in AxesDatabaseHandler.user_login_failed.
# AXES_LOCKOUT_PARAMETERS only changes how these rows are READ/aggregated.
# ---------------------------------------------------------------------------
ROWS = [
    # username, ip,           user_agent, failures
    ("alice", "10.0.0.1", UA_CHROME, 3),   # R1
    ("alice", "10.0.0.2", UA_CHROME, 3),   # R2  (same user, second address)
    ("bob",   "10.0.0.1", UA_CHROME, 5),   # R3  (already at limit)
    ("carol", "10.0.0.9", UA_FIREFOX, 5),  # R4
    ("carol", "10.0.0.9", UA_SAFARI, 2),   # R7  (same user+ip, other UA)
    ("dave",  "10.0.0.5", UA_CHROME, 2),   # R5
]

for username, ip, ua, failures in ROWS:
    AccessAttempt.objects.create(
        username=username,
        ip_address=ip,
        user_agent=ua,
        http_accept="text/html",
        path_info="/admin/login/",
        get_data="",
        post_data="",
        failures_since_start=failures,
        attempt_time=now(),
    )

# Probe clients = the (username, ip, UA) a new login attempt would present.
PROBES = [
    ("alice", "10.0.0.1", UA_CHROME),   # P1 known address
    ("alice", "10.0.0.2", UA_CHROME),   # P2 second known address
    ("alice", "10.0.0.77", UA_CHROME),  # P3 brand-new address, no row
    ("bob",   "10.0.0.1", UA_CHROME),   # P4
    ("carol", "10.0.0.9", UA_FIREFOX),  # P5 exact UA match
    ("carol", "10.0.0.9", UA_CHROME),   # P6 different UA than stored rows
]


def build_request(username, ip, ua):
    request = HttpRequest()
    request.method = "POST"
    request.META["REMOTE_ADDR"] = ip
    request.META["HTTP_USER_AGENT"] = ua
    request.META["PATH_INFO"] = "/admin/login/"
    return request, {"username": username}


def evaluate(config):
    """Return per-probe read conditions, SQL, matched rows, count, decision."""
    with override_settings(AXES_LOCKOUT_PARAMETERS=config):
        results = []
        for username, ip, ua in PROBES:
            request, credentials = build_request(username, ip, ua)
            filter_list = get_client_parameters(
                username, ip, ua, request, credentials
            )
            cache_keys = get_client_cache_keys(request, credentials)
            querysets = [
                AccessAttempt.objects.filter(**kw) for kw in filter_list
            ]
            # Mirror AxesDatabaseHandler.get_failures: max of Sum() per queryset
            from django.db.models import Sum

            per_qs = [
                qs.aggregate(t=Sum("failures_since_start"))["t"] or 0
                for qs in querysets
            ]
            failures = max(per_qs)
            matched = _matched_union(filter_list)
            results.append({
                "probe": (username, ip, ua),
                "filter_kwargs": filter_list,
                "cache_keys": cache_keys,
                "sql": [str(qs.query) for qs in querysets],
                "matched": matched,
                "failures": failures,
                "locked": failures >= FAILURE_LIMIT,
            })
        return results


def _matched_union(filter_list):
    seen, out = set(), []
    for kw in filter_list:
        for row in AccessAttempt.objects.filter(**kw):
            if row.pk not in seen:
                seen.add(row.pk)
                out.append((row.username, row.ip_address, row.user_agent,
                            row.failures_since_start))
    return out


def hr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


hr("PHYSICAL ROWS STORED IN SQLite (write key is independent of lockout config)")
print(AccessAttempt.objects.values_list(
    "username", "ip_address", "user_agent", "failures_since_start"))
print("unique_together =", AccessAttempt._meta.unique_together)

for label, config in (("OLD " + str(OLD), OLD), ("NEW " + str(NEW), NEW)):
    hr(f"{label}  |  failure limit = {FAILURE_LIMIT}")
    for r in evaluate(config):
        u, ip, ua = r["probe"]
        print(f"\nprobe username={u:<6} ip={ip:<10} ua={ua}")
        print("  read filter_kwargs :", r["filter_kwargs"])
        print("  cache key(s)       :", r["cache_keys"])
        print("  SQL                :", " ".join(r["sql"]).split("WHERE")[1].strip())
        print("  matched rows       :", r["matched"])
        print("  aggregated failures:", r["failures"],
              "=> LOCKED" if r["locked"] else "=> allowed")

# ---------------------------------------------------------------------------
# Reset scope: run the real handler reset inside a rolled-back transaction so
# the shared dataset is never actually destroyed.
# ---------------------------------------------------------------------------
hr("ADMIN RESET SCOPE (real handler, rolled back afterwards)")
handler = AxesDatabaseHandler()


def preview_reset(description, **kwargs):
    with transaction.atomic():
        before = set(AccessAttempt.objects.values_list(
            "username", "ip_address", "user_agent"))
        deleted = handler.reset_attempts(**kwargs)
        after = set(AccessAttempt.objects.values_list(
            "username", "ip_address", "user_agent"))
        print(f"\n{description}  kwargs={kwargs}")
        print("  rows removed:", sorted(before - after))
        print("  delete count:", deleted)
        transaction.set_rollback(True)


preview_reset("reset by username only  (axes_reset_username alice)",
              username="alice")
preview_reset("reset by ip only        (axes_reset_ip 10.0.0.9)",
              ip_address="10.0.0.9")
preview_reset("reset by ip AND username(axes_reset_ip_username, default AND)",
              ip_address="10.0.0.1", username="alice")
preview_reset("reset ip OR username    (ip_or_username=True)",
              ip_address="10.0.0.1", username="alice", ip_or_username=True)

# Cache handler limitation relevant to the OR-style reset path.
from axes.handlers.cache import AxesCacheHandler
hr("CACHE-HANDLER RESET LIMITATION")
try:
    AxesCacheHandler().reset_attempts(ip_address="10.0.0.1",
                                      username="alice", ip_or_username=True)
except NotImplementedError as exc:
    print("ip_or_username=True raises NotImplementedError:", exc)

# ---------------------------------------------------------------------------
# Asserted expectations: one row per probe, machine-checked lock decision and
# matched-row count for BOTH configurations. Exits non-zero on any mismatch.
# ---------------------------------------------------------------------------
hr("ASSERTED EXPECTATIONS")
# key: (username, ip, ua) -> (old_locked, old_matched, new_locked, new_matched)
EXPECT = {
    ("alice", "10.0.0.1", UA_CHROME): (True, 2, False, 1),   # split -> no lock
    ("alice", "10.0.0.2", UA_CHROME): (True, 2, False, 1),   # split -> no lock
    ("alice", "10.0.0.77", UA_CHROME): (True, 2, False, 0),  # old row still locks; new key empty
    ("bob", "10.0.0.1", UA_CHROME): (True, 1, True, 1),      # unchanged
    ("carol", "10.0.0.9", UA_FIREFOX): (True, 2, True, 2),   # UAs merged in both
    ("carol", "10.0.0.9", UA_CHROME): (True, 2, True, 2),    # UA not in read key
}
old_results = {(r["probe"]): r for r in evaluate(OLD)}
new_results = {(r["probe"]): r for r in evaluate(NEW)}

failures = 0
for probe, (o_lock, o_match, n_lock, n_match) in EXPECT.items():
    o, n = old_results[probe], new_results[probe]
    checks = [
        ("old locked", o["locked"], o_lock),
        ("old matched", len(o["matched"]), o_match),
        ("new locked", n["locked"], n_lock),
        ("new matched", len(n["matched"]), n_match),
        ("cache keys differ", o["cache_keys"] != n["cache_keys"], True),
    ]
    for name, got, want in checks:
        status = "ok" if got == want else "FAIL"
        if got != want:
            failures += 1
        print(f"[{status}] {probe} {name}: got={got} want={want}")

# The write key never changes between configurations.
assert AccessAttempt._meta.unique_together == (("username", "ip_address", "user_agent"),)
# Under the new config, cache key is derived strictly from username+ip: the two
# carol probes differ only by UA and must collapse to the SAME key.
carol_key_firefox = tuple(new_results[("carol", "10.0.0.9", UA_FIREFOX)]["cache_keys"])
carol_key_chrome = tuple(new_results[("carol", "10.0.0.9", UA_CHROME)]["cache_keys"])
assert carol_key_firefox == carol_key_chrome, "new cache key must ignore user agent"
# And distinct username+ip pairs must produce distinct keys.
pairs = [(p[0], p[1]) for p in EXPECT]
pair_keys = {pair: tuple(new_results[probe]["cache_keys"])
             for probe, pair in zip(EXPECT, pairs)}
assert len(set(pair_keys.values())) == len(set(pairs)), "one key per username+ip pair"

print("\nTOTAL ASSERTION FAILURES:", failures)
raise SystemExit(1 if failures else 0)
