from django.core.management.base import BaseCommand

from payments import jobs


class Command(BaseCommand):
    help = "Daily reconciliation. Opens ReconciliationExceptions for anything that needs a human."

    def handle(self, *args, **options):
        found = jobs.reconcile()
        self.stdout.write("reconcile: " + (", ".join(f"{k}={v}" for k, v in found.items()) or "clean"))
