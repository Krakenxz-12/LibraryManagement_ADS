import time

from django.core.management.base import BaseCommand, CommandError

from library_app.services import expire_stale_holds


class Command(BaseCommand):
    help = (
        "Continuously check for expired reservation and borrow-request holds "
        "and release them automatically."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=60,
            help="Seconds to wait between checks (default: 60).",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run one expiration check and exit.",
        )

    def handle(self, *args, **options):
        interval = options["interval"]
        if interval < 1:
            raise CommandError("--interval must be at least 1 second.")

        if options["once"]:
            self._run_expiration_check()
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Hold-expiry worker started. Checking every {interval} seconds."
            )
        )

        try:
            while True:
                self._run_expiration_check()
                time.sleep(interval)
        except KeyboardInterrupt:
            self.stdout.write("\nHold-expiry worker stopped.")

    def _run_expiration_check(self):
        try:
            count = expire_stale_holds()
            if count:
                self.stdout.write(
                    self.style.SUCCESS(f"Released {count} expired hold(s).")
                )
        except Exception as error:
            self.stderr.write(
                self.style.ERROR(f"Hold-expiry check failed: {error}")
            )
