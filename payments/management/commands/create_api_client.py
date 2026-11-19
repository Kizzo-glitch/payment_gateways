from django.core.management.base import BaseCommand, CommandError

from payments.models import ApiClient


class Command(BaseCommand):
    help = ("Create an API client and print its key ONCE. "
            "Use --rotate to issue a new key for an existing client.")

    def add_arguments(self, parser):
        parser.add_argument("name", help='e.g. "Fedha-Grow (local)"')
        parser.add_argument("--rotate", action="store_true",
                            help="Replace the key of an existing client (the old key stops working).")

    def handle(self, *args, name, rotate, **options):
        existing = ApiClient.objects.filter(name=name).first()
        if rotate:
            if existing is None:
                raise CommandError(f"No client named {name!r}.")
            raw = existing.rotate_key()
        else:
            if existing is not None:
                raise CommandError(f"{name!r} already exists. Use --rotate for a new key.")
            _client, raw = ApiClient.create_with_key(name)
        self.stdout.write(self.style.SUCCESS(f"API key for {name}:"))
        self.stdout.write(raw)
        self.stdout.write("Store it now (e.g. in the client's environment). It can't be shown again.")
