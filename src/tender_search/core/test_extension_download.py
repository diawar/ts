from io import BytesIO
import json
from zipfile import ZipFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from .extension_views import FILES


@override_settings(STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class ExtensionDownloadTests(TestCase):
    def test_download_requires_login_and_contains_installable_files_only(self):
        self.assertEqual(self.client.get('/settings/extension/download/').status_code,302)
        self.client.force_login(get_user_model().objects.create_user('extension-user'))
        response=self.client.get('/settings/extension/download/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Content-Type'],'application/zip')
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()),{'goszakup-assistant/'+name for name in FILES})
            manifest=json.loads(archive.read('goszakup-assistant/manifest.json'))
            self.assertEqual(manifest['manifest_version'],3)
            self.assertEqual(manifest['version'],'0.17.0')
            self.assertEqual(set(manifest['permissions']),{'activeTab','scripting','storage','alarms'})
            self.assertEqual(manifest['background']['service_worker'],'background.js')
            self.assertEqual(manifest['host_permissions'], ['https://zakup.gov.kz/*'])
            self.assertFalse(manifest['content_scripts'][0]['all_frames'])
            self.assertEqual(manifest['content_scripts'][0]['run_at'], 'document_start')
            self.assertNotIn('https://zakup.gov.kz/*',manifest['content_scripts'][0]['matches'])
        self.assertContains(self.client.get('/settings/'),'Загрузить распакованное расширение')
        self.assertContains(self.client.get('/settings/'),'/settings/extension/download/')
        self.assertEqual(self.client.post('/settings/extension/download/').status_code,405)

    @override_settings(ALLOWED_HOSTS=['app.example.test'])
    def test_download_pairs_bridge_with_own_server_origin(self):
        self.client.force_login(get_user_model().objects.create_user('server-extension-user'))
        response=self.client.get('/settings/extension/download/',secure=True,HTTP_HOST='app.example.test')
        with ZipFile(BytesIO(response.content)) as archive:
            config=archive.read('goszakup-assistant/bridge-config.js').decode()
            self.assertIn('https://app.example.test',config)
            manifest=json.loads(archive.read('goszakup-assistant/manifest.json'))
            self.assertIn('https://app.example.test/lots/*/preparation/*',manifest['content_scripts'][0]['matches'])
            self.assertNotIn('<all_urls>',json.dumps(manifest))
