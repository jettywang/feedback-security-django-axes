from django.core.management.base import BaseCommand

from axes.utils import reset


class Command(BaseCommand):
    help = "Reset all access attempts and lockouts for given IP addresses"

    def add_arguments(self, parser):
        parser.add_argument("ip", nargs="+", type=str)
        parser.add_argument(
            "--scope",
            type=str,
            default=None,
            help="Only reset attempts for the given authentication entry point scope.",
        )

    def handle(self, *args, **options):
        count = 0

        for ip in options["ip"]:
            count += reset(ip=ip, scope=options["scope"])

        if count:
            self.stdout.write(f"{count} attempts removed.")
        else:
            self.stdout.write("No attempts found.")
