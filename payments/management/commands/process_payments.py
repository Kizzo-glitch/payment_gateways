from django.core.management.base import BaseCommand

from payments import jobs


class Command(BaseCommand):
    help = "Expire stale OTPs, poll providers for pending payments, flag stuck ones. Run every minute."

    def handle(self, *args, **options):
        expired = jobs.expire_otps()
        polled = jobs.poll_pending()
        stuck = jobs.flag_stuck()
        self.stdout.write(f"expired={expired} polled={polled} flagged_stuck={stuck}")
