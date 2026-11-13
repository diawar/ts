import time
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from tender_search.core.auth_mail import cleanup, deliver_one


class Command(BaseCommand):
    help = 'Deliver queued authentication emails with retries; never log mail contents.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')

    def handle(self, *args, **options):
        last_cleanup = 0
        while True:
            close_old_connections()
            try:
                if time.monotonic()-last_cleanup > 3600:
                    cleanup()
                    last_cleanup = time.monotonic()
                did_work = deliver_one()
            except Exception as error:
                self.stderr.write('Auth mail worker: '+type(error).__name__)
                if options['once']:
                    raise
                time.sleep(5)
                continue
            if options['once'] and not did_work:
                break
            if not did_work:
                time.sleep(3)
