from django.test import TestCase,override_settings
from .models import CollectionLot,UserLotWorkflow,Favorite,AnnouncementWatch
from .workflow_state import update_workflow

@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class LotTriageTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        update_workflow(self.owner.pk,self.lot.pk,stage='new')
        self.item=CollectionLot.objects.get(profile__owner=self.owner,lot=self.lot)
        self.url=f'/workspace/items/{self.item.pk}/stage/'
        self.back='/workspace/?profile=all&stage=all&sort=-amount&page=2'

    def test_buttons_after_specification_only_for_own_untriaged_lot(self):
        response=self.client.get(f'/lots/{self.lot.pk}/',{'next':self.back})
        self.assertContains(response,'id="lot-triage-actions"')
        page=response.content.decode()
        self.assertGreater(page.index('id="lot-triage-actions"'),page.index('id="lot-specification"'))
        self.assertContains(response,'Неинтересно')
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(f'/lots/{self.lot.pk}/'),'id="lot-triage-actions"')

    def test_interesting_returns_to_same_list_and_syncs_favorite(self):
        response=self.client.post(self.url,{'stage':'preparing','return_to':self.back})
        self.assertEqual(response.url,self.back)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertTrue(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())

    def test_uninteresting_stops_personal_tracking_and_redirect_is_safe(self):
        response=self.client.post(self.url,{'stage':'not_participated','return_to':'https://example.com/workspace/'})
        self.assertTrue(response.url.startswith('/workspace/'))
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'not_participated')
        self.assertFalse(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())
        self.assertFalse(AnnouncementWatch.objects.filter(owner=self.owner,announcement=self.lot.announcement,enabled=True).exists())
