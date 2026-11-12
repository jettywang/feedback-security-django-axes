from django.core.management.base import BaseCommand

from axes.handlers.proxy import AxesProxyHandler


class Command(BaseCommand):
    help = (
        "Clean up to --batch-size expired access attempt records per run. "
        "Re-run the command while the reported remaining count is greater than zero."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help="Maximum number of expired access attempts to delete in this batch",
        )

    def handle(self, *args, **options):
        deleted, remaining = AxesProxyHandler.clean_expired_user_attempts_batch(
            batch_size=options["batch_size"]
        )
        if deleted or remaining:
            self.stdout.write(
                f"{deleted} expired attempts removed. {remaining} expired attempts remaining."
            )
        else:
            self.stdout.write("No expired attempts found.")
