from io import BytesIO
from tempfile import TemporaryDirectory
from decimal import Decimal
from django.test import TestCase,override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from pypdf import PdfWriter
from .models import LotShipment,UserLotWorkflow,CompanySettings,UserLotPrice
from .workflow_state import update_workflow
from .fulfillment import advance

@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ShippingFlowTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        config=override_settings(MEDIA_ROOT=self.tmp.name);config.enable();self.addCleanup(config.disable)
        update_workflow(self.owner.pk,self.lot.pk,stage='delivery')

    def stage(self):
        return UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage

    def pdf(self, marker=None):
        writer=PdfWriter();writer.add_blank_page(width=100,height=100)
        if marker:
            writer.add_metadata({'/Subject': marker})
        output=BytesIO();writer.write(output)
        return SimpleUploadedFile('act.pdf',output.getvalue(),content_type='application/pdf')

    def test_courier_act_payment_archive_and_owner_only_download(self):
        url=reverse('shipment_save',args=[self.lot.pk])
        self.client.post(url,{'mode':'courier','courier':'CDEK','tracking_number':'TEST123'})
        shipment=LotShipment.objects.get(owner=self.owner,lot=self.lot)
        self.assertIsNotNone(shipment.shipped_at)
        self.assertEqual(shipment.tracking_number,'TEST123')
        self.assertEqual(self.stage(),'delivery')
        self.client.post(reverse('shipment_act',args=[self.lot.pk]),{'act':self.pdf()})
        self.assertEqual(self.stage(),'payment')
        shipment.refresh_from_db()
        download=reverse('shipment_act_download',args=[self.lot.pk])+f'?act={shipment.pk}'
        response=self.client.get(download)
        self.assertEqual(response.status_code,200)
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF-'))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(download).status_code,404)
        self.assertNotContains(self.client.get(reverse('lot_detail',args=[self.lot.pk])),f'?act={shipment.pk}')
        self.assertNotEqual(UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage,'payment')
        with self.assertRaises(ValueError):advance(self.owner.pk,self.lot.pk,'paid')
        self.assertEqual(self.stage(),'payment')
        self.assertTrue(LotShipment.objects.get(pk=shipment.pk).act)

    def test_invalid_upload_and_act_before_dispatch_cannot_move_stage(self):
        endpoint=reverse('shipment_act',args=[self.lot.pk])
        self.client.post(endpoint,{'act':self.pdf()})
        self.assertEqual(self.stage(),'delivery')
        self.assertFalse(LotShipment.objects.exists())
        self.client.post(reverse('shipment_save',args=[self.lot.pk]),{'mode':'own'})
        self.client.post(endpoint,{'act':SimpleUploadedFile('act.pdf',b'<script>bad</script>')})
        self.assertEqual(self.stage(),'delivery')
        self.assertFalse(LotShipment.objects.get().act)
        with self.assertRaises(ValueError):advance(self.owner.pk,self.lot.pk,'payment')

    def test_other_user_cannot_change_personal_delivery(self):
        self.client.force_login(self.other)
        self.client.post(reverse('shipment_save',args=[self.lot.pk]),{'mode':'own'})
        self.assertFalse(LotShipment.objects.exists())

    def test_act_download_rejects_malformed_identifiers(self):
        endpoint=reverse('shipment_act_download',args=[self.lot.pk])
        for identifier in ('','wrong','-1','9'*30):
            self.assertEqual(self.client.get(endpoint,{'act':identifier}).status_code,404)

    def upload_for(self, owner, marker):
        self.client.force_login(owner)
        update_workflow(owner.pk,self.lot.pk,stage='delivery')
        self.client.post(reverse('shipment_save',args=[self.lot.pk]),{'mode':'own'})
        upload=self.pdf(marker)
        content=upload.read();upload.seek(0)
        response=self.client.post(reverse('shipment_act',args=[self.lot.pk]),{
            'act':upload, 'owner':self.owner.pk, 'shipment_id':0})
        self.assertEqual(response.status_code,302)
        shipment=LotShipment.objects.get(owner=owner,lot=self.lot)
        self.assertEqual(UserLotWorkflow.objects.get(owner=owner,lot=self.lot).stage,'payment')
        return shipment,content

    def test_two_owners_same_lot_and_filename_have_independent_acts_and_stages(self):
        first,first_content=self.upload_for(self.owner,'Owner A')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.other,lot=self.lot).stage,'preparing')
        second,second_content=self.upload_for(self.other,'Owner B')
        self.assertNotEqual(first.act.name,second.act.name)
        self.assertNotEqual(first_content,second_content)
        endpoint=reverse('shipment_act_download',args=[self.lot.pk])
        for owner,own,foreign,expected in ((self.owner,first,second,first_content),
                                          (self.other,second,first,second_content)):
            self.client.force_login(owner)
            response=self.client.get(endpoint,{'act':own.pk})
            self.assertEqual(response.status_code,200)
            self.assertEqual(b''.join(response.streaming_content),expected)
            self.assertIn('private',response['Cache-Control'])
            self.assertIn('no-store',response['Cache-Control'])
            self.assertEqual(self.client.get(endpoint,{'act':foreign.pk}).status_code,404)
            page=self.client.get(reverse('lot_detail',args=[self.lot.pk]))
            self.assertEqual(list(page.context['shared_acts']),[own])
            self.assertContains(page,f'?act={own.pk}')
            self.assertNotContains(page,f'?act={foreign.pk}')

    def test_replacing_own_act_does_not_change_or_delete_another_owners_file(self):
        first,first_content=self.upload_for(self.owner,'Owner A')
        second,_=self.upload_for(self.other,'Owner B')
        old_name=second.act.name
        update_workflow(self.other.pk,self.lot.pk,stage='delivery')
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('shipment_act',args=[self.lot.pk]),{'act':self.pdf('Owner B replacement')})
        first.refresh_from_db();second.refresh_from_db()
        self.assertNotEqual(second.act.name,old_name)
        self.assertFalse(second.act.storage.exists(old_name))
        self.assertTrue(first.act.storage.exists(first.act.name))
        self.assertTrue(second.act.storage.exists(second.act.name))
        self.client.force_login(self.owner)
        response=self.client.get(reverse('shipment_act_download',args=[self.lot.pk]),{'act':first.pk})
        self.assertEqual(response.status_code,200)
        self.assertEqual(b''.join(response.streaming_content),first_content)

    def test_act_is_not_public_or_readable_by_outsider_or_other_administrator(self):
        from django.contrib.auth import get_user_model
        shipment,_=self.upload_for(self.owner,'Private file')
        endpoint=reverse('shipment_act_download',args=[self.lot.pk])
        self.client.logout()
        self.assertEqual(self.client.get(endpoint,{'act':shipment.pk}).status_code,302)
        outsider=get_user_model().objects.create_user('act-outsider')
        administrator=get_user_model().objects.create_superuser('act-administrator')
        for actor in (outsider,administrator):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(endpoint,{'act':shipment.pk}).status_code,404)
            self.assertNotContains(self.client.get(reverse('lot_detail',args=[self.lot.pk])),f'?act={shipment.pk}')
            for prefix in ('/media/','/','/static/','/data/documents/'):
                self.assertEqual(self.client.get(prefix+shipment.act.name).status_code,404)

    def test_zero_vat_ignores_submitted_rate_and_locked_price_keeps_rate(self):
        update_workflow(self.owner.pk,self.lot.pk,stage='preparing')
        company=CompanySettings.objects.create(owner=self.owner,is_vat_payer=False)
        self.client.post(self.endpoint,{'confirm_discount':'on','unit_gross':'116','vat_rate':'16'})
        price=UserLotPrice.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(price.vat_rate,0)
        from .lot_pricing import pricing_context
        self.assertEqual(pricing_context(self.owner,self.lot)['price_result']['total_net'],Decimal('348'))
        company.is_vat_payer=True;company.save()
        # Once fixed for a submitted bid, company setting changes cannot rewrite its tax basis.
        from django.utils import timezone
        price.locked_at=timezone.now();price.save()
        self.assertFalse(pricing_context(self.owner,self.lot)['price_has_vat'])
