from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver
from .models import Favorite, CollectionLot
from django.db import transaction

@receiver(post_delete,sender=Favorite)
def stop_watch_when_removed(sender,instance,**kwargs):
    from .status_monitor import cancel_watch
    from .workflow_state import syncing
    if not syncing.get():
        cancel_watch(instance.owner_id,instance.lot.announcement_id)

    from .tasks import cleanup_favorite_documents
    announcement_id = instance.lot.announcement_id
    def enqueue_cleanup():
        try:
            cleanup_favorite_documents.delay(announcement_id)
        except Exception:
            from .document_service import cleanup_documents
            cleanup_documents(announcement_id)
    transaction.on_commit(enqueue_cleanup)


@receiver(post_save,sender=Favorite)
def download_when_favorited(sender,instance,created,**kwargs):
    if created:
        from .models import CollectionLot
        from .workflow_state import syncing, add_to_participation
        if not syncing.get():
            add_to_participation(instance.owner_id, instance.lot_id)


@receiver(post_save, sender=CollectionLot)
def workflow_saved(sender, instance, raw=False, created=False, update_fields=None, **kwargs):
    if not raw:
        from .workflow_state import sync_lot, publish_workflow
        publish_workflow(instance, created=created, update_fields=update_fields)
        sync_lot(instance.profile.owner_id, instance.lot_id)


@receiver(pre_delete, sender=CollectionLot)
def remember_workflow_owner(sender, instance, **kwargs):
    instance._workflow_owner_id = instance.profile.owner_id


@receiver(post_delete, sender=CollectionLot)
def workflow_deleted(sender, instance, **kwargs):
    from .workflow_state import sync_lot
    sync_lot(instance._workflow_owner_id, instance.lot_id)
