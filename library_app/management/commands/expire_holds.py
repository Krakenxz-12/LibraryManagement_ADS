from django.core.management.base import BaseCommand

from library_app.services import expire_stale_holds


class Command(BaseCommand):
    help = "Release borrow-request and reservation holds that were not collected within the pickup window."

    def handle(self, *args, **options):
        count = expire_stale_holds()
        self.stdout.write(self.style.SUCCESS(f"Expired {count} uncollected hold(s)."))
