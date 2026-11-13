import time
from django.core.management import BaseCommand
from django.db import close_old_connections
from tender_search.core.collections_service import schedule_collections
from tender_search.core.draft_queue import expire_draft_queue
from tender_search.core.outcome_service import schedule_outcomes, deliver_outcomes, move_closed_unsubmitted
from tender_search.core.contract_service import schedule_contracts
from tender_search.core.technical_documents import schedule as schedule_documents
from tender_search.core.document_service import cleanup_expired_documents
from tender_search.core.workflow_notifications import deliver_workflow_notifications
from tender_search.core.fulfillment import archive_due


class Command(BaseCommand):
    help = 'Schedule saved collection refreshes around the clock, independently of Telegram.'

    def handle(self, *args, **options):
        while True:
            close_old_connections()
            try:
                schedule_collections()
            except Exception:
                self.stderr.write('Collection scheduling unavailable; retrying.')
            for operation in (expire_draft_queue, move_closed_unsubmitted, archive_due, schedule_outcomes, deliver_outcomes, schedule_contracts, deliver_workflow_notifications, schedule_documents, cleanup_expired_documents):
                try:
                    operation()
                except Exception:
                    self.stderr.write('Outcome monitoring unavailable; retrying.')
            time.sleep(15)
