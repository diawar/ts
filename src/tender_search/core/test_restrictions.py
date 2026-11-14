from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from .models import Announcement, Lot, CompanySettings, UserLotWorkflow, ApplicationPreparation
from .restrictions import save_restriction_report
from .templatetags.search_tags import participation_warning


class RestrictionReportTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('restriction-owner')
        CompanySettings.objects.create(owner=self.owner, company_bin='241040001415')
        ann = Announcement.objects.create(source_id=41022921, external_id=17921264, number='17921264-1')
        self.lot = Lot.objects.create(announcement=ann, source_id=61773760, number='88544080-ЗЦП1')
        UserLotWorkflow.objects.create(owner=self.owner, lot=self.lot, stage='preparing')
        self.report = dict(schema='goszakup-restriction-v1', company_bin='241040001415',
            announcement_number=ann.number, lot_number=self.lot.number, observed_at=timezone.now().isoformat(),
            national_withdrawn=True, source_kind='lot_api',
            source_url='https://zakup.gov.kz/api/v1/announcements/17921264/lots')

    def test_shared_evidence_preserves_workflow_and_no_preparation_created(self):
        self.client.force_login(self.owner)
        import json
        response=self.client.post('/settings/extension/report/',data=json.dumps(self.report),content_type='application/json')
        self.assertEqual(response.status_code,200)
        self.lot.refresh_from_db()
        self.assertIn('Изъятие из национального режима',participation_warning(self.lot))
        self.assertEqual(UserLotWorkflow.objects.get(owner=self.owner,lot=self.lot).stage,'preparing')
        self.assertFalse(ApplicationPreparation.objects.exists())
        self.report.update(national_withdrawn=False, observed_at=timezone.now().isoformat())
        save_restriction_report(self.owner,self.lot.pk,self.report)
        self.lot.refresh_from_db()
        self.assertEqual(participation_warning(self.lot),'')

    def test_rejects_wrong_lot_bin_source_missing_flag_and_stale_report(self):
        for change in [dict(national_withdrawn=None),dict(national_withdrawn='false'),dict(company_bin='111111111111'),
                dict(lot_number='999-ЗЦП1'),dict(source_url='https://other.test/'),dict(source_kind='guess')]:
            with self.assertRaises(ValueError):save_restriction_report(self.owner,self.lot.pk,dict(self.report,**change))
        save_restriction_report(self.owner,self.lot.pk,self.report)
        with self.assertRaises(ValueError):save_restriction_report(self.owner,self.lot.pk,self.report)
