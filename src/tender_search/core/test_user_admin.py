from .test_status_watch import set_watch
from django.contrib.auth import get_user_model
from django.test import TestCase, Client, override_settings


@override_settings(STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class UserAdministrationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('admin', password='initial-secret-72!')
        cls.member = get_user_model().objects.create_user('member')
        cls.other_admin = get_user_model().objects.create_superuser('other-admin')

    def data(self, **changes):
        return dict({'username': 'new-user', 'first_name': 'Новый', 'email': 'new@example.com',
                     'password1': 'new-account-secret-752!', 'password2': 'new-account-secret-752!'}, **changes)

    def test_admin_role_has_menu_and_server_access(self):
        self.assertEqual(self.client.get('/administration/').status_code, 302)
        for user in (self.member,):
            self.client.force_login(user)
            self.assertNotContains(self.client.get('/main/'), 'href="/administration/"')
            self.assertEqual(self.client.get('/administration/').status_code, 403)
            self.assertEqual(self.client.post('/administration/', self.data()).status_code, 403)
            self.assertEqual(self.client.get('/search/index-status/').status_code, 403)
        self.assertEqual(get_user_model().objects.count(), 3)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get('/main/'), 'href="/administration/"')
        response = self.client.get('/administration/')
        self.assertContains(response, 'Все пользователи')
        self.assertEqual(set(response.context['users'].values_list('username', flat=True)),
                         {'admin', 'member', 'other-admin'})
        self.assertNotContains(response, self.admin.password)

    def test_create_regular_user_with_hashed_password_and_no_privilege_escalation(self):
        self.client.force_login(self.admin)
        response = self.client.post('/administration/', self.data(is_staff='1', is_superuser='1'))
        self.assertRedirects(response, '/administration/')
        user = get_user_model().objects.get(username='new-user')
        self.assertTrue(user.check_password(self.data()['password1']))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.email, 'new@example.com')

    def test_invalid_duplicate_and_mismatched_passwords_do_not_create_users(self):
        self.client.force_login(self.admin)
        for data in (self.data(username='ADMIN'), self.data(password2='different'),
                     self.data(password1='123', password2='123'),
                     self.data(email=''), self.data(email='invalid-address')):
            response = self.client.post('/administration/', data)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertNotContains(response, 'value="'+data['password1']+'"')
        self.assertEqual(get_user_model().objects.count(), 3)

    def test_email_is_unique_on_creation_and_editing_ignoring_case(self):
        self.member.email = 'Existing@Example.com'
        self.member.save()
        self.client.force_login(self.admin)
        response = self.client.post('/administration/', self.data(email=' existing@EXAMPLE.com '))
        self.assertContains(response, 'Этот e-mail уже используется другим аккаунтом.')
        self.assertFalse(get_user_model().objects.filter(username='new-user').exists())
        response = self.client.post(self.edit_path(self.other_admin), {
            'username': self.other_admin.username, 'role': 'admin',
            'is_active': 'on', 'email': 'EXISTING@example.com'})
        self.assertContains(response, 'Этот e-mail уже используется другим аккаунтом.')
        self.other_admin.refresh_from_db()
        self.assertEqual(self.other_admin.email, '')
        response = self.client.post(self.edit_path(self.member), {
            'username': self.member.username, 'role': 'user',
            'is_active': 'on', 'email': ' EXISTING@Example.com '})
        self.assertRedirects(response, '/administration/')
        self.member.refresh_from_db()
        self.assertEqual(self.member.email, 'existing@example.com')

    def test_database_rejects_duplicate_email_including_surrounding_spaces(self):
        from django.db import IntegrityError, transaction
        self.member.email = 'member@example.com'
        self.member.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user('duplicate-email', email=' MEMBER@example.com ')

    def test_csrf_is_required_even_for_admin(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/administration/', self.data()).status_code, 403)

    def edit_path(self, user):
        return f'/administration/users/{user.pk}/edit/'

    def delete_path(self, user):
        return f'/administration/users/{user.pk}/delete/'

    def test_edit_and_delete_access_and_csrf(self):
        for path in (self.edit_path(self.member), self.delete_path(self.member)):
            self.assertEqual(self.client.get(path).status_code, 302)
            for actor in (self.member,):
                self.client.force_login(actor)
                self.assertEqual(self.client.get(path).status_code, 403)
                self.assertEqual(self.client.post(path, {'confirm': 'on'}).status_code, 403)
            self.client.logout()
            client = Client(enforce_csrf_checks=True)
            client.force_login(self.admin)
            self.assertEqual(client.post(path, {'confirm': 'on'}).status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())

    def test_edit_preserves_password_and_privileges(self):
        self.client.force_login(self.admin)
        password = self.member.password
        response = self.client.post(self.edit_path(self.member), {
            'role': 'user', 'username': 'renamed', 'first_name': 'Имя', 'last_name': 'Фамилия',
            'email': 'changed@example.com', 'is_active': 'on', 'is_superuser': 'on', 'is_staff': 'on'})
        self.assertRedirects(response, '/administration/')
        self.member.refresh_from_db()
        self.assertEqual(self.member.username, 'renamed')
        self.assertEqual(self.member.email, 'changed@example.com')
        self.assertEqual(self.member.password, password)
        self.assertFalse(self.member.is_superuser)
        self.assertFalse(self.member.is_staff)
        self.client.post(self.edit_path(self.member), {'username': 'renamed', 'role': 'user'})
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_password_reset_validates_and_invalidates_old_session(self):
        self.member.set_password('previous-secret-916!')
        self.member.save()
        old_session = Client()
        old_session.force_login(self.member)
        self.client.force_login(self.admin)
        path = self.edit_path(self.member)
        for changes in ({'username': 'ADMIN'}, {'password1': '123', 'password2': '123'},
                        {'password1': 'new-secret-916!', 'password2': 'mismatch'},
                        {'password2': 'only-confirmation'}):
            response = self.client.post(path, {'username': self.member.username, 'role': 'user', 'is_active': 'on', **changes})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.member.refresh_from_db()
            self.assertTrue(self.member.check_password('previous-secret-916!'))
        response = self.client.post(path, {'username': self.member.username, 'role': 'user', 'is_active': 'on',
            'password1': 'new-secret-916!', 'password2': 'new-secret-916!'})
        self.assertRedirects(response, '/administration/')
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_password('new-secret-916!'))
        self.assertEqual(old_session.get('/main/').status_code, 302)

    def test_admin_cannot_be_removed_disabled_or_renamed(self):
        self.client.force_login(self.admin)
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(self.delete_path(self.admin), {'confirm': 'on'}).status_code, 403)
        response = self.client.post(self.edit_path(self.admin), {'username': 'different', 'role': 'user',
            'first_name': 'Администратор', 'password1': 'replacement-admin-882!', 'password2': 'replacement-admin-882!'})
        self.assertRedirects(response, '/administration/')
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.username, 'admin')
        self.assertTrue(self.admin.is_active)
        self.assertTrue(self.admin.is_superuser)
        self.assertTrue(self.admin.check_password('replacement-admin-882!'))
        self.assertEqual(self.client.get('/administration/').status_code, 200)

    def test_delete_requires_confirmation_and_removes_personal_data_only(self):
        from .models import (SearchProfile, TelegramSubscription, Announcement,
                             Lot, Favorite, AnnouncementWatch, StatusDelivery)
        own = SearchProfile.objects.create(owner=self.member, name='Personal', include_terms=['mouse'], enabled=True)
        other = SearchProfile.objects.create(owner=self.admin, name='Keep', include_terms=['keyboard'])
        subscription = TelegramSubscription.objects.create(owner=self.member, enabled=True)
        announcement = Announcement.objects.create(source_id=123, name='Shared announcement')
        lot = Lot.objects.create(source_id=123, announcement=announcement, name='Shared lot')
        Favorite.objects.get_or_create(owner=self.member, lot=lot)[0]
        kept_favorite = Favorite.objects.get_or_create(owner=self.admin, lot=lot)[0]
        watch = set_watch(owner=self.member, announcement=announcement,
                                                lot=lot, enabled=True)
        delivery = StatusDelivery.objects.create(watch=watch, event_key='test-pending')
        self.client.force_login(self.admin)
        path = self.delete_path(self.member)
        self.assertContains(self.client.get(path), 'Отменить удаление нельзя')
        self.assertEqual(self.client.post(path, {}).status_code, 200)
        self.assertTrue(get_user_model().objects.filter(pk=self.member.pk).exists())
        self.assertRedirects(self.client.post(path, {'confirm': 'on'}), '/administration/')
        self.assertFalse(get_user_model().objects.filter(pk=self.member.pk).exists())
        self.assertFalse(SearchProfile.objects.filter(pk=own.pk).exists())
        self.assertFalse(TelegramSubscription.objects.filter(pk=subscription.pk).exists())
        self.assertTrue(SearchProfile.objects.filter(pk=other.pk).exists())
        self.assertTrue(Favorite.objects.filter(pk=kept_favorite.pk).exists())
        self.assertTrue(Lot.objects.filter(pk=lot.pk).exists())
        self.assertFalse(AnnouncementWatch.objects.filter(pk=watch.pk).exists())
        self.assertFalse(StatusDelivery.objects.filter(pk=delivery.pk).exists())
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_roles_grant_and_revoke_access_in_existing_session(self):
        member_client = Client()
        member_client.force_login(self.member)
        self.client.force_login(self.other_admin)
        self.assertContains(self.client.get('/administration/'), 'Все пользователи')
        self.assertEqual(self.client.get(self.edit_path(self.member)).context['form']['role'].value(), 'user')
        for role, staff, superuser in [('admin', True, True), ('user', False, False)]:
            response = self.client.post(self.edit_path(self.member), {
                'username': self.member.username, 'is_active': 'on', 'role': role})
            self.assertRedirects(response, '/administration/')
            self.member.refresh_from_db()
            self.assertEqual((self.member.is_staff, self.member.is_superuser), (staff, superuser))
            self.assertEqual(member_client.get('/administration/').status_code, 200 if superuser else 403)
            self.assertEqual(self.client.get(self.edit_path(self.member)).context['form']['role'].value(), role)
        response = self.client.post(self.edit_path(self.member), {
            'username': self.member.username, 'is_active': 'on', 'role': 'invalid'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('role', response.context['form'].errors)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)
        self.assertFalse(self.member.is_staff)

    def test_member_cannot_assign_admin_role_to_self(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(self.edit_path(self.member), {
            'username': self.member.username, 'is_active': 'on', 'role': 'admin'}).status_code, 403)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_superuser)

    def test_legacy_staff_is_shown_as_user_and_cannot_be_selected(self):
        self.member.is_staff = True
        self.member.save(update_fields=['is_staff'])
        self.client.force_login(self.admin)
        response = self.client.get(self.edit_path(self.member))
        self.assertEqual(list(response.context['form'].fields['role'].choices),
                         [('user', 'Пользователь'), ('admin', 'Администратор')])
        self.assertEqual(response.context['form']['role'].value(), 'user')
        self.assertNotContains(self.client.get('/administration/'), 'Сотрудник')
        data = {'username': self.member.username, 'is_active': 'on', 'role': 'staff'}
        self.assertIn('role', self.client.post(self.edit_path(self.member), data).context['form'].errors)
        data['role'] = 'user'
        self.assertRedirects(self.client.post(self.edit_path(self.member), data), '/administration/')
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_staff)
        self.assertFalse(self.member.is_superuser)

    def test_telegram_subscribers_are_visible_only_to_admins(self):
        from .models import TelegramContact,TelegramSubscription
        TelegramContact.objects.create(chat_id=123456,username='telegram_member',first_name='<script>Имя</script>')
        TelegramContact.objects.create(chat_id=654321,username='unlinked_visitor',enabled=False)
        TelegramSubscription.objects.create(owner=self.member,chat_id=123456,enabled=True)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get('/administration/').status_code,403)
        for admin in (self.admin,self.other_admin):
            self.client.force_login(admin)
            page=self.client.get('/administration/')
            self.assertContains(page,'Telegram nick')
            self.assertContains(page,'Telegram name')
            self.assertContains(page,'@telegram_member')
            self.assertNotContains(page,'unlinked_visitor')
            self.assertContains(page,'&lt;script&gt;Имя&lt;/script&gt;')
            self.assertNotContains(page,'Подписчики Telegram')
            accounts={user.username:user for user in page.context['users']}
            self.assertEqual(accounts['member'].telegram_status,'Подключён')
            self.assertEqual(accounts['member'].telegram_nick,'telegram_member')
            self.assertEqual(accounts['admin'].telegram_status,'Не подключён')
            self.assertEqual(accounts['admin'].telegram_nick,'')
            self.assertNotIn('<section',__import__('bs4').BeautifulSoup(page.content,'html.parser').title.get_text())
