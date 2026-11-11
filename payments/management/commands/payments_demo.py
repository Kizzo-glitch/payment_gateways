from django.core.management.base import BaseCommand

from payments import demo


class Command(BaseCommand):
    help = ("Run the settlement-options demo against the simulator. "
            "Resets simulator data only; never touches real providers.")

    def handle(self, *args, **options):
        demo.run(out=self.stdout.write)
