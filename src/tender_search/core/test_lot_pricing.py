from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import UserLotPrice, AnnouncementLotCache, SearchProfile, CollectionLot, Favorite, AnnouncementWatch
from .lot_pricing import verified_pricing, calculate_price, LotPriceForm
from .search_service import store_lot
from .test_index import row

@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class LotPricingTests(TestCase):
    def setUp(self):
        self.owner=get_user_model().objects.create_user('price-owner')
        self.other=get_user_model().objects.create_user('other-price-owner')
        self.client.force_login(self.owner)
        self.lot=store_lot(row(1,system_id=1,purchase_method_id=12))
        self.lot.refresh_state='done';self.lot.checked_at=timezone.now();self.lot.save()
        self.detail={'id':self.lot.source_id,'system':{'id':1},'is_combined':False,
            'plan_items':[{'count':3,'measure':{'name':'Штука'},'purchase_method':{'id':12},'system':{'id':1}}]}
        self.cache=AnnouncementLotCache.objects.create(pk=self.lot.announcement.source_id,rows=[self.detail])
        self.endpoint=f'/lots/{self.lot.pk}/price/'
        for owner in (self.owner,self.other):
            profile=SearchProfile.objects.create(owner=owner,name='Мои')
            CollectionLot.objects.create(profile=profile,lot=self.lot,stage='interesting')

    def test_gate_rejects_unknown_external_combined_and_conflicting_data(self):
        from .workflow_state import update_workflow
        for user in (self.owner,self.other):update_workflow(user.pk,self.lot.pk,stage='preparing')
        self.assertEqual(verified_pricing(self.lot)['quantity'],3)
        for system in [2,3,6,7,None]:
            self.lot.raw['system_id']=system
            self.detail['system']={'id':system}
            self.detail['plan_items'][0]['system']={'id':system}
            self.cache.rows=[self.detail];self.cache.save()
            self.assertIsNone(verified_pricing(self.lot))
        self.lot.raw['system_id']=1
        self.detail['system']={'id':1};self.detail['plan_items'][0]['system']={'id':1}
        self.detail['is_combined']=True;self.cache.rows=[self.detail];self.cache.save()
        self.assertIsNone(verified_pricing(self.lot))
        self.detail['is_combined']=False
        self.detail['plan_items']*=2;self.cache.rows=[self.detail];self.cache.save()
        self.assertIsNone(verified_pricing(self.lot))
        self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'116','vat_rate':'16'}).status_code,403)
        self.assertFalse(UserLotPrice.objects.exists())

    def test_card_cache_is_scoped_to_goszakup_in_mixed_pages(self):
        from .lot_pricing import attach_lot_budgets, cached_pricing_detail
        medical = store_lot(row(self.lot.source_id,
            announcement_id=self.lot.announcement.source_id, _source='med_ecc'))
        attach_lot_budgets([self.lot, medical])
        self.assertEqual(self.lot._budget_display['quantity'], 3)
        self.assertEqual(medical._budget_display, {})
        self.assertIsNone(cached_pricing_detail(medical))
        medical.refresh_from_db()
        self.assertIsNone(cached_pricing_detail(medical))

    def test_unknown_method_source_and_missing_card_do_not_enable_entry(self):
        self.lot.raw['purchase_method_id']=38
        self.assertIsNone(verified_pricing(self.lot))
        self.lot.raw['purchase_method_id']=12
        self.lot.announcement.source='med_ecc'
        self.assertIsNone(verified_pricing(self.lot))
        self.lot.announcement.source='zakup'
        self.cache.delete()
        self.assertIsNone(verified_pricing(self.lot))
        response=self.client.get(f'/lots/{self.lot.pk}/')
        self.assertNotContains(response,'class="lot-price-form"')

    def test_money_rounding_and_totals_use_portal_direct_calculation(self):
        result=calculate_price(Decimal('116.00'),Decimal('16'),Decimal('3'))
        self.assertEqual((result['unit_net'],result['unit_vat'],result['total_net'],result['total_vat'],result['total_gross']),
            (Decimal('100.00'),Decimal('16.00'),Decimal('300.00'),Decimal('48.00'),Decimal('348.00')))
        rounded=calculate_price(Decimal('125'),Decimal('16'),Decimal('333'))
        self.assertEqual(rounded['total_gross'],Decimal('41625.53'))
        self.assertEqual(rounded['rounding_difference'],Decimal('0.53'))
        fractional=calculate_price(Decimal('116'),Decimal('16'),Decimal('0.125'))
        self.assertEqual(fractional['total_gross'],Decimal('14.50'))
        self.assertEqual(calculate_price(Decimal('100'),Decimal('0'))['unit_vat'],0)
        self.assertEqual(calculate_price(Decimal('105'),Decimal('5'))['unit_net'],100)

    def test_application_price_is_private_and_locked_without_changing_favorites(self):
        from .workflow_state import update_workflow
        for user in (self.owner,self.other):update_workflow(user.pk,self.lot.pk,stage='preparing')
        self.assertEqual(self.client.get(self.endpoint).status_code,405)
        response=self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'1 160,00','vat_rate':'16','next':'/workspace/?profile=all'})
        self.assertEqual(response.status_code,302)
        price=UserLotPrice.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(price.unit_gross,1160)
        page=self.client.get(f'/lots/{self.lot.pk}/preparation/')
        self.assertEqual(page.context['price_result']['unit_net'],1000)
        self.assertContains(page,'data-price-output="total_gross"')
        self.client.force_login(self.other)
        other_page=self.client.get(f'/lots/{self.lot.pk}/preparation/')
        self.assertEqual(other_page.context['price_form']['unit_gross'].value(),'')
        self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'232','vat_rate':'16'})
        price.refresh_from_db();self.assertEqual(price.unit_gross,1160)
        self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'','vat_rate':'16'}).status_code,400)
        self.assertEqual(UserLotPrice.objects.get(owner=self.other).unit_gross,232)
        self.assertTrue(Favorite.objects.filter(owner=self.owner,lot=self.lot).exists())
        self.assertTrue(AnnouncementWatch.objects.filter(owner=self.owner,enabled=True).exists())
        self.assertTrue(UserLotPrice.objects.filter(owner=self.owner).exists())
        self.client.logout();self.assertEqual(self.client.post(self.endpoint,{}).status_code,302)

    def test_invalid_prices_are_rejected_and_posted_rate_cannot_override_16(self):
        from .workflow_state import update_workflow
        for user in (self.owner,self.other):update_workflow(user.pk,self.lot.pk,stage='preparing')
        UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross=116,vat_rate=16)
        for value in ['-1','0','NaN','Infinity','1e3','10.123','9'*21]:
            response=self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':value,'vat_rate':'16'})
            self.assertEqual(response.status_code,400,value)
        self.assertEqual(self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'116','vat_rate':'13'}).status_code,302)
        self.assertEqual(UserLotPrice.objects.get(owner=self.owner).vat_rate,16)
        self.assertEqual(UserLotPrice.objects.get(owner=self.owner).unit_gross,116)

    def test_missing_or_invalid_count_never_becomes_one(self):
        for count in [None,0,-1,'NaN','Infinity','abc','0.0000001']:
            self.detail['plan_items'][0]['count']=count
            self.cache.rows=[self.detail];self.cache.save()
            self.assertIsNone(verified_pricing(self.lot)['quantity'])
        self.assertNotIn('total_gross',calculate_price(Decimal('116'),Decimal('16')))
