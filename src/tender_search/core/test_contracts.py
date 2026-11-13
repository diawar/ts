from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone
from .contract_source import contract_evidence, ContractReview
from .contract_service import check_contract, schedule_contracts
from .models import ContractWatch, UserLotWorkflow, CompanySettings, LotOutcomeWatch, CollectionLot
from .workflow_state import update_workflow
from .fulfillment import advance


class ContractEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.bin = '241040001415'
        self.lot = SimpleNamespace(source_id=10, number='10-ЗЦП1', announcement=SimpleNamespace(source_id=20,number='20-1'))
        self.card = {'id':30, 'system':{'id':1}, 'supplier_organization':{'iin_bin':self.bin},
            'announcement':{'announcement_number':'20-1'}, 'contract_type':{'id':1},
            'root_contract':{'id':30}, 'status':{'id':6,'name':'Действует'},
            'sign_date':(self.now-timedelta(days=1)).timestamp(), 'contract_number_sys':'contract', 'currency':{'egz_code':'KZT'}}
        self.subject = {'id':40,'lot':{'id':10,'lot_number':'10-ЗЦП1'}, 'contract':{'id':30},
            'contract_price_with_vat':116, 'actual_payment_sum_with_vat':0,'actual_execution_date':None}
        self.summary = {'id':30,'system_id':1,'supplier_organization_id':50,'announcement_number':'20-1'}
        self.api = Mock()
        self.api.announcement_lots.return_value=[{'id':10,'lot_number':'10-ЗЦП1','status':{'id':16}}]
        def get(path, params=None):
            if path.endswith('organizations/'):
                return {'count':1,'next':None,'results':[{'id':50,'iin_bin':self.bin}]}
            if path.endswith('contracts/'):
                return {'count':1,'next':None,'results':[self.summary]}
            if path.endswith('subjects/'):
                return [self.subject]
            return self.card
        self.api.get_json.side_effect=get

    def result(self):
        return contract_evidence(self.api,self.lot,self.bin,self.now)

    def test_signing_execution_and_paid_are_distinct(self):
        self.card['status']['id']=18
        self.assertEqual(self.result()['state'],'contract')
        self.card['status']['id']=6
        self.assertEqual(self.result()['state'],'delivery')
        self.subject['actual_payment_sum_with_vat']=116
        self.assertEqual(self.result()['state'],'delivery')  # Advance payment alone.
        self.subject['actual_execution_date']=(self.now-timedelta(hours=1)).timestamp()
        self.assertEqual(self.result()['state'],'payment')
        self.card['status']['id']=11
        self.subject['actual_payment_sum_with_vat']=100
        self.assertEqual(self.result()['state'],'payment')
        self.subject['actual_payment_sum_with_vat']=116
        self.assertEqual(self.result()['state'],'paid')

    def test_mismatch_and_ambiguous_amendments_never_advance(self):
        original=deepcopy(self.card)
        for change in [{'supplier_organization':{'iin_bin':'000000000000'}},
                       {'announcement':{'announcement_number':'other'}},
                       {'system':{'id':3}}, {'contract_type':{'id':2}},
                       {'status':{'id':25}}, {'sign_date':None}]:
            self.card=dict(original,**change)
            with self.assertRaises(ContractReview):self.result()
        self.card=original
        self.subject['lot']['lot_number']='other'
        self.assertEqual(self.result()['state'],'waiting')

    def test_appeal_and_unknown_payment_hold(self):
        self.api.announcement_lots.return_value[0]['status']['id']=23
        with self.assertRaises(ContractReview):self.result()
        self.api.announcement_lots.return_value[0]['status']['id']=16
        self.subject['actual_execution_date']=(self.now-timedelta(hours=1)).timestamp()
        self.subject['actual_payment_sum_with_vat']='NaN'
        with self.assertRaises(ContractReview):self.result()


    def test_incomplete_registry_does_not_choose_first_contract(self):
        original = self.api.get_json.side_effect
        def incomplete(path, params=None):
            data = original(path,params)
            if path.endswith('contracts/'):
                data['count']=2;data['next']='next-page'
            return data
        self.api.get_json.side_effect=incomplete
        with self.assertRaises(ContractReview):self.result()

    def test_second_contract_for_same_lot_requires_review(self):
        original = self.api.get_json.side_effect
        def multiple(path, params=None):
            data = deepcopy(original(path,params))
            if path.endswith('contracts/'):
                data['results'].append(dict(self.summary,id=31));data['count']=2
            elif '/31/' in path:
                if path.endswith('subjects/'):
                    data[0]['contract']['id']=31
                else:
                    data['id']=31;data['root_contract']['id']=31
            return data
        self.api.get_json.side_effect=multiple
        with self.assertRaises(ContractReview):self.result()


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},
    'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ContractWorkflowTests(TestCase):
    def setUp(self):
        from .test_lot_pricing import LotPricingTests
        LotPricingTests.setUp(self)
        # Contract evidence tests start after a confirmed win, independently of price entry.
        from .models import UserLotPrice
        UserLotPrice.objects.create(owner=self.owner,lot=self.lot,unit_gross=116,vat_rate=16,locked_at=timezone.now())
        CompanySettings.objects.update_or_create(owner=self.owner,defaults={'company_bin':'241040001415'})
        self.state=update_workflow(self.owner.pk,self.lot.pk,stage='contract',outcome='won',result_tracking=True)
        from .outcome_service import register_watch
        self.verdict=register_watch(self.owner.pk,self.lot.pk)
        self.verdict.state='won';self.verdict.company_bin='241040001415';self.verdict.winner_bin='241040001415'
        self.verdict.checked_at=timezone.now();self.verdict.save()
        self.watch=ContractWatch.objects.create(workflow=self.state,lease='test')

    def run_check(self, result, side_effect=None):
        ContractWatch.objects.filter(pk=self.watch.pk).update(lease='test')
        with patch('tender_search.core.contract_service.supported_source',return_value=True), \
             patch('tender_search.core.contract_service.source_client'), \
             patch('tender_search.core.contract_service.contract_evidence',side_effect=side_effect,return_value=result):
            check_contract(self.watch.pk,'test')
        self.state.refresh_from_db();self.watch.refresh_from_db()

    def test_confirmed_progress_is_exclusive_and_won_only_after_completion(self):
        from .models import LotShipment
        LotShipment.objects.create(owner=self.owner,lot=self.lot,act='private-acts/accepted.pdf',
            shipped_at=timezone.now(),act_uploaded_at=timezone.now())
        for target in ('contract','delivery','payment','paid'):
            self.run_check({'state':target,'number':'contract','status':'status'})
            self.assertEqual(self.state.stage,'won' if target=='paid' else target)
            self.assertEqual(self.state.outcome,'won')
            page=self.client.get('/workspace/',{'stage':'won'})
            self.assertEqual(page.context['page'].paginator.count,1 if target=='paid' else 0)
        self.assertTrue(self.state.completed_at)
        self.assertFalse(LotOutcomeWatch.objects.get(pk=self.verdict.pk).enabled)
        page=self.client.get('/workspace/',{'stage':'won'})
        self.assertEqual(page.context['page'].paginator.count,1)
        with patch('tender_search.core.tasks.check_lot_contract.delay') as task:
            schedule_contracts();task.assert_not_called()

    def test_payment_evidence_requires_attached_act(self):
        for target in ('payment','paid'):
            self.run_check({'state':target,'number':'contract','status':'status'})
            self.assertEqual(self.state.stage,'delivery')
            self.assertFalse(self.state.completed_at)

    def test_archive_during_request_fences_response(self):
        def archive(*args):
            update_workflow(self.owner.pk,self.lot.pk,stage='lost',outcome='lost')
            advance(self.owner.pk,self.lot.pk,'archive')
            return {'state':'delivery'}
        self.run_check({},side_effect=archive)
        self.assertEqual(self.state.stage,'archived')

    def test_bin_change_during_request_fences_response(self):
        from .outcome_service import reset_company
        def change(*args):
            reset_company(self.owner.pk,'123456789012')
            return {'state':'delivery'}
        self.run_check({},side_effect=change)
        self.assertEqual(self.state.stage,'contract')
        self.assertEqual(self.state.outcome,'')

    def test_no_fresh_verdict_no_advance(self):
        self.verdict.checked_at=timezone.now()-timedelta(days=2);self.verdict.save()
        self.run_check({'state':'delivery'})
        self.assertEqual(self.state.stage,'contract')
        self.assertEqual(self.watch.state,'review')

    def test_new_bin_clears_previous_result_without_rewinding_delivery(self):
        from .outcome_service import reset_company
        update_workflow(self.owner.pk,self.lot.pk,stage='delivery')
        reset_company(self.owner.pk,'123456789012')
        self.state.refresh_from_db()
        self.assertEqual(self.state.stage,'delivery')
        self.assertEqual(self.state.outcome,'')

    def test_protocol_recheck_preserves_delivery_and_revised_result_holds(self):
        from .outcome_service import check_outcome
        from .outcome_source import Outcome
        update_workflow(self.owner.pk,self.lot.pk,stage='delivery')
        for result, expected in [(Outcome('won',winner_bin='241040001415'),'won'),(Outcome('review'),'')]:
            LotOutcomeWatch.objects.filter(pk=self.verdict.pk).update(lease='protocol')
            with patch('tender_search.core.outcome_service.supported_source',return_value=True), \
                 patch('tender_search.core.outcome_service.source_client'), \
                 patch('tender_search.core.outcome_service.OutcomePortal') as portal:
                portal.return_value.outcome.return_value=result
                check_outcome(self.verdict.pk,'protocol')
            self.state.refresh_from_db()
            self.assertEqual(self.state.stage,'delivery')
            self.assertEqual(self.state.outcome,expected)

    def test_review_preserves_stage_and_ordinary_user_bin_banner(self):
        self.run_check({},side_effect=ContractReview('Требуется проверка'))
        self.assertEqual(self.state.stage,'contract')
        self.assertContains(self.client.get('/workspace/',{'stage':'contract'}),'Требуется проверка')
        self.client.force_login(self.other)
        CompanySettings.objects.update_or_create(owner=self.other, defaults={'company_bin': ''})
        self.assertContains(self.client.get('/settings/'),'Укажите БИН организации')
        self.client.post('/settings/',{'action':'company','company_bin':'123456789012'})
        self.assertNotContains(self.client.get('/settings/'),'Укажите БИН организации')
        self.assertEqual(CompanySettings.objects.get(owner=self.owner).company_bin,'241040001415')
