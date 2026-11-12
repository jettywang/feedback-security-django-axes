from django.core.management.base import BaseCommand

from axes.utils import reset


class Command(BaseCommand):
    help = "Reset all access attempts and lockouts for a given authentication entry point scope"

    def add_arguments(self, parser):
        parser.add_argument("scope", type=str)

    def handle(self, *args, **options):
        count = reset(scope=options["scope"])

        if count:
            self.stdout.write(f"{count} attempts removed.")
        else:
            self.stdout.write("No attempts found.")
