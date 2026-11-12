import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from axes.lockout_stats import (
    STATISTIC_STATES,
    collect_lockout_statistics,
    summarize_statistics,
)
from axes.models import AccessAttempt


class Command(BaseCommand):
    help = "List access attempts or summarize lockout statistics by effective rule"

    def add_arguments(self, parser):
        parser.add_argument(
            "--ip-address",
            dest="ip_address",
            default=None,
            help="Only include attempts for the given IP address.",
        )
        parser.add_argument(
            "--username",
            dest="username",
            default=None,
            help="Only include attempts for the given username.",
        )
        parser.add_argument(
            "--statistics",
            action="store_true",
            help="Only report counts per effective lockout rule, without account identifiers.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Emit the statistics as machine-readable JSON (requires --statistics).",
        )

    def handle(self, *args, **options):
        if options["json_output"] and not options["statistics"]:
            raise CommandError("--json can only be used together with --statistics.")

        if options["statistics"]:
            groups = collect_lockout_statistics(
                username=options["username"],
                ip_address=options["ip_address"],
            )
            if options["json_output"]:
                self.stdout.write(json.dumps(self._build_json(groups)))
            else:
                self._write_text_statistics(groups)
            return

        attempts = AccessAttempt.objects.all()
        if options["username"] is not None:
            attempts = attempts.filter(username=options["username"])
        if options["ip_address"] is not None:
            attempts = attempts.filter(ip_address=options["ip_address"])

        for obj in attempts:
            self.stdout.write(
                f"{obj.ip_address}\t{obj.username}\t{obj.failures_since_start}"
            )

    def _build_json(self, groups):
        return {
            "generated_at": timezone.now().isoformat(),
            "summary": summarize_statistics(groups),
            "groups": [
                {
                    "rule": self._rule_data(group.rule),
                    "counts": {
                        state: group.counts[state] for state in STATISTIC_STATES
                    },
                }
                for group in groups
            ],
        }

    def _write_text_statistics(self, groups):
        totals = summarize_statistics(groups)
        self.stdout.write(
            "Lockout statistics: "
            f"{totals['locked']} locked, "
            f"{totals['expired']} expired, "
            f"{totals['failed']} failed"
        )
        for group in groups:
            rule = group.rule
            rule_name = "tier" if rule.is_tier else "failure limit"
            self.stdout.write(
                f"{rule_name} {rule.threshold} "
                f"(cooloff {self._format_cooloff(rule.cooloff)}): "
                f"locked={group.counts['locked']} "
                f"expired={group.counts['expired']} "
                f"failed={group.counts['failed']}"
            )

    @staticmethod
    def _rule_data(rule):
        return {
            "type": "tier" if rule.is_tier else "failure_limit",
            "threshold": rule.threshold,
            "cooloff_seconds": (
                int(rule.cooloff.total_seconds())
                if rule.cooloff is not None
                else None
            ),
        }

    @staticmethod
    def _format_cooloff(cooloff):
        if cooloff is None:
            return "permanent"
        return str(timedelta(seconds=int(cooloff.total_seconds())))
