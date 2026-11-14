import json
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from . import test_extension_transfer
from .models import DraftQueueItem, ApplicationPreparation, UserLotWorkflow


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class DraftQueueTests(TestCase):
    def setUp(self):
        test_extension_transfer.ExtensionTransferTests.setUp(self)
        from .models import CompanySettings
        CompanySettings.objects.filter(owner=self.owner).update(taxpayer_type='Юридическое лицо')
        self.lot.raw={'system_id':1,'purchase_method_id':12,'_pricing_detail':{
            'id':self.lot.source_id,'is_combined':False,'plan_items':[{'id':1,'purchase_method':{'id':12}}]}}
        self.lot.save()
        self.client.post(f'/lots/{self.lot.pk}/preparation/',self.data)
        self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk)]})
        self.item=DraftQueueItem.objects.get(owner=self.owner,lot=self.lot)
        self.base=f'/workspace/drafts/{self.item.pk}/'

    def post_progress(self,**changes):
        snapshot={'status':'complete','lotConfirmed':True,'description_saved':True,'number':'123',
            'observed_at':timezone.now().isoformat(),'company_bin':'241040001415',
            'announcement_number':self.lot.announcement.number,'lot_number':self.lot.number,'message':'Готово',**changes}
        return self.client.post(self.base+'progress/',json.dumps({'generation':str(self.item.generation),'snapshot':snapshot}),content_type='application/json')

    def test_enqueue_is_durable_unique_and_does_not_create_portal_draft(self):
        self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk),str(self.lot.pk)]})
        self.assertEqual(DraftQueueItem.objects.filter(owner=self.owner).count(),1)
        p=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(p.portal_state,'unconfirmed')
        self.assertContains(self.client.get('/workspace/drafts/'),'Ожидает')
        self.assertContains(self.client.get('/workspace/drafts/'),'В работе')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')

    def test_packet_registry_rows_do_not_require_repeat_consent(self):
        response=self.client.post(self.base+'packet/',{'launch_guard':'1'})
        self.assertEqual(response.status_code,200)
        self.item.refresh_from_db()
        self.assertEqual(response.json()['queue_context']['generation'],str(self.item.generation))
        self.assertNotIn('repeat_product',response.json()['packet'])
        self.client.post(self.base+'guard/',json.dumps({'generation':str(self.item.generation),'release':True,'idle':True}),content_type='application/json')
        self.lot.raw['_pricing_detail']['plan_items'].append({'id':2,'purchase_method':{'id':12}});self.lot.save()
        self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,200)
        self.item.refresh_from_db()
        self.client.post(self.base+'guard/',json.dumps({'generation':str(self.item.generation),'release':True,'idle':True}),content_type='application/json')
        self.assertEqual(self.client.post(self.base+'packet/',{'repeat_product':'on','launch_guard':'1'}).status_code,200)
        UserLotWorkflow.objects.filter(owner=self.owner,lot=self.lot).update(stage='applied')
        self.assertEqual(self.client.post(self.base+'packet/',{'repeat_product':'on','launch_guard':'1'}).status_code,400)

    def test_progress_saves_description_without_submitting_and_is_idempotent(self):
        observed=timezone.now().isoformat()
        self.assertEqual(self.post_progress(observed_at=observed).status_code,200)
        self.assertEqual(self.post_progress(observed_at=observed).status_code,200)
        self.item.refresh_from_db();self.assertEqual(self.item.state,'complete')
        p=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(p.portal_number,'123');self.assertIsNotNone(p.portal_description_saved_at)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'awaiting_results')
        self.assertEqual(self.post_progress(status='running').status_code,400)

    def test_progress_cannot_confirm_wrong_lot_or_partial_description(self):
        self.assertEqual(self.post_progress(lot_number='999-ЗЦП1').status_code,400)
        self.assertEqual(self.post_progress(description_saved=False).status_code,400)
        self.assertEqual(self.post_progress(lotConfirmed=False).status_code,400)
        self.item.refresh_from_db();self.assertEqual(self.item.state,'queued')
        self.assertEqual(self.post_progress(status='blocked',message='Сайт недоступен').status_code,200)
        self.assertContains(self.client.get('/workspace/drafts/'),'Сайт недоступен')

    def test_private_queue_and_csrf(self):
        strict=Client(enforce_csrf_checks=True);strict.force_login(self.owner)
        self.assertEqual(strict.post(self.base+'packet/',{'launch_guard':'1'}).status_code,403)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get('/workspace/drafts/'),'data-queue-id=')
        self.assertEqual(self.client.post(self.base+'packet/',{'launch_guard':'1'}).status_code,404)
        self.assertEqual(self.post_progress().status_code,400)

    def test_cancelled_generation_cannot_receive_old_browser_reports(self):
        old=str(self.item.generation)
        self.client.post(self.base+'cancel/')
        self.assertEqual(self.post_progress().status_code,400)
        self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk)]})
        self.item.refresh_from_db();self.assertNotEqual(str(self.item.generation),old)
        self.assertEqual(self.item.state,'queued')

    def test_running_item_cannot_be_removed_as_if_stopped(self):
        self.assertEqual(self.post_progress(status='running').status_code,200)
        self.client.post(self.base+'cancel/')
        self.item.refresh_from_db();self.assertEqual(self.item.state,'running')

    def test_partial_draft_keeps_number_without_claiming_description_complete(self):
        self.assertEqual(self.post_progress(status='blocked',description_saved=False,message='Выбор страны не подтверждён').status_code,200)
        p=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(p.portal_number,'123');self.assertIsNone(p.portal_description_saved_at)
        self.item.refresh_from_db();self.assertEqual(self.item.state,'blocked')
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')

    def test_malformed_progress_and_stale_generation_are_rejected(self):
        for payload in ([],{'snapshot':None},{'snapshot':{},'generation':'old'}):
            self.assertEqual(self.client.post(self.base+'progress/',json.dumps(payload),content_type='application/json').status_code,400)

    def test_changed_company_cannot_reuse_previous_description_confirmation(self):
        from .models import CompanySettings
        observed=timezone.now().isoformat()
        self.assertEqual(self.post_progress(observed_at=observed).status_code,200)
        CompanySettings.objects.filter(owner=self.owner).update(company_bin='123456789012')
        self.assertEqual(self.post_progress(company_bin='123456789012',observed_at=observed).status_code,400)


    def test_waiting_for_recovery_keeps_queue_and_can_be_stopped(self):
        response=self.post_progress(status='waiting',number='',lotConfirmed=False,description_saved=False,message='Ожидание восстановления')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['label'],'Ожидание восстановления')
        self.client.post(self.base+'cancel/')
        self.item.refresh_from_db();self.assertEqual(self.item.state,'waiting')
        self.assertEqual(self.post_progress(status='paused',number='',lotConfirmed=False,description_saved=False).status_code,200)
        self.client.post(self.base+'cancel/')
        self.item.refresh_from_db();self.assertEqual(self.item.state,'cancelled')

    def test_portal_check_is_personal_and_does_not_reset_or_create(self):
        self.assertEqual(self.post_progress().status_code,200)
        url=self.base+'check/'
        prepared=self.client.post(url).json()
        target=prepared['target']
        self.assertEqual(target['number'],'123')
        result={**target,'check_state':'absent','observed_at':timezone.now().isoformat(),'message':'Нет заявок'}
        response=self.client.post(url,json.dumps({'generation':prepared['generation'],'result':result}),content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.item.refresh_from_db()
        self.assertEqual(self.item.state,'complete')
        self.assertEqual(self.item.portal_check['state'],'absent')
        self.assertEqual(ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot).portal_number,'123')
        result['number']='999'
        self.assertEqual(self.client.post(url,json.dumps({'generation':prepared['generation'],'result':result}),content_type='application/json').status_code,400)
        self.client.logout()
        self.assertEqual(self.client.post(url).status_code,302)

    def test_restart_requires_recent_absence_and_preserves_preparation(self):
        self.assertEqual(self.post_progress().status_code,200)
        old_generation=str(self.item.generation)
        self.client.post(self.base+'restart/',{'generation':old_generation})
        self.item.refresh_from_db();self.assertEqual(self.item.state,'complete')
        self.item.portal_check={'state':'absent','number':'123','checked_at':timezone.now().isoformat()};self.item.save()
        before=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot).data
        self.client.post(self.base+'restart/',{'generation':old_generation})
        self.item.refresh_from_db();self.assertEqual(self.item.state,'queued')
        self.assertNotEqual(str(self.item.generation),old_generation)
        prep=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(prep.data,before);self.assertEqual(prep.portal_number,'')
        packet=self.client.post(self.base+'packet/',{'launch_guard':'1'}).json()['packet']
        self.item.refresh_from_db()
        self.assertEqual(packet['reset_absent_application'],'123')
        self.assertEqual(packet['existing_application_number'],'')
        self.assertEqual(self.post_progress().status_code,200)
        self.assertNotIn('reset_absent_application',self.client.post(self.base+'packet/',{'launch_guard':'1'}).json().get('packet',{}))

    def test_expired_queue_leaves_preparation_stage_and_preserves_saved_data(self):
        from datetime import timedelta
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1)
        self.lot.announcement.save()
        old_generation=self.item.generation
        response=self.client.post(self.base+'packet/',{'launch_guard':'1'})
        self.assertTrue(response.json()['skipped'])
        self.item.refresh_from_db();self.assertEqual(self.item.state,'cancelled')
        self.assertNotEqual(self.item.generation,old_generation)
        self.assertTrue(ApplicationPreparation.objects.filter(owner=self.owner,lot=self.lot).exists())
        workflow=UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot)
        self.assertEqual(workflow.stage,'awaiting_results')
        self.assertTrue(workflow.result_tracking)
        page=self.client.get('/workspace/drafts/')
        self.assertNotIn(self.item.pk,[item.pk for item in page.context['items']])
        self.assertEqual(next(step['count'] for step in page.context['steps'] if step['key']=='preparing'),0)
        self.client.post('/workspace/drafts/add/',{'lots':[str(self.lot.pk)]})
        self.item.refresh_from_db();self.assertEqual(self.item.state,'cancelled')

    def test_expiry_repairs_already_cancelled_or_completed_queue_without_claiming_nonparticipation(self):
        from datetime import timedelta
        from .draft_queue import expire_draft_queue
        from .workflow_state import update_workflow
        from .models import CollectionLot, LotOutcomeWatch
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1)
        self.lot.announcement.save()
        before=ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot).data
        for state in ('cancelled','complete'):
            with self.subTest(state=state):
                DraftQueueItem.objects.filter(pk=self.item.pk).update(state=state)
                update_workflow(self.owner.pk,self.lot.pk,stage='preparing',result_tracking=True)
                expire_draft_queue(self.owner)
                self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'awaiting_results')
                self.assertFalse(CollectionLot.objects.filter(profile__owner=self.owner,lot=self.lot).exclude(stage='awaiting_results').exists())
                self.assertTrue(LotOutcomeWatch.objects.get(owner=self.owner,lot=self.lot).enabled)
                self.assertEqual(ApplicationPreparation.objects.get(owner=self.owner,lot=self.lot).data,before)
                self.item.refresh_from_db()
                self.assertEqual(self.item.state,state)

    def test_expiry_preserves_open_preparations_and_later_workflow_stages(self):
        from datetime import timedelta
        from .draft_queue import expire_draft_queue
        from .workflow_state import update_workflow
        expire_draft_queue(self.owner)
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.lot.announcement.deadline=timezone.now()-timedelta(seconds=1)
        self.lot.announcement.save()
        for stage in ('applied','contract','delivery','payment','archived'):
            update_workflow(self.owner.pk,self.lot.pk,stage=stage)
            expire_draft_queue(self.owner)
            self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,stage)
