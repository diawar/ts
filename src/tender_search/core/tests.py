from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings

from .models import Announcement, Favorite, Lot, Notification, SearchProfile
from .models import WebSearch, SearchHit
from .forms import SearchForm
from .search_service import execute_search, store_lot, refresh_lot_data, resolve_customer
from tender_search.client import Page, SourceError
from django.utils import timezone


def sample_lot(identifier=10, name="Телефон", description="Сотовая связь"):
    return {"id": identifier, "announcement_id": 100, "announcement_number": "100-1",
            "name_ru": name, "description_ru": description, "total_price": 10000,
            "lot_number": f"{identifier}-ЗЦП1", "status_name": "Опубликован", "customers": [],
            "offer_end_date": "2026-10-29T12:00:00+05:00"}


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user("owner", password="test-strong-password")
        cls.other = get_user_model().objects.create_user("other", password="test-strong-password")
        cls.admin = get_user_model().objects.create_superuser("administrator", password="test-strong-password")
        SearchProfile.objects.create(owner=cls.other, name="Чужой поиск", include_terms=["кабель"])
        Notification.objects.create(owner=cls.other, dedup_key="1", message="Чужое уведомление")

    def test_anonymous_dashboard_redirects_to_login(self):
        self.assertContains(self.client.get("/"), 'Регистрация')
        self.assertRedirects(self.client.get("/main/"), "/login/?next=/main/")

    def test_user_sees_only_own_counts(self):
        self.client.force_login(self.owner)
        response = self.client.get("/main/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["profiles"], 0)
        self.assertEqual(response.context["notifications"], 0)
        self.assertNotContains(response, "Чужой")

    def test_user_cannot_access_admin(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/admin/").status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/").status_code, 403)
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 403)
        self.assertEqual(self.client.post("/admin/login/").status_code, 403)

    def test_login_works_and_external_redirect_is_ignored(self):
        response = self.client.post("/login/", {"username": "owner", "password": "test-strong-password",
                                                 "next": "https://evil.example/"})
        self.assertRedirects(response, "/main/")

    def test_csrf_is_required_and_get_cannot_log_out(self):
        secure_client = Client(enforce_csrf_checks=True)
        self.assertEqual(secure_client.post("/login/", {"username": "owner"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get("/logout/").status_code, 405)

    def test_health_has_no_private_details(self):
        self.assertEqual(self.client.get("/health/").json(), {"status": "ok"})

    def test_duplicate_favorite_is_prevented(self):
        announcement = Announcement.objects.create(source_id=1, name="Закупка")
        lot = Lot.objects.create(source_id=1, announcement=announcement, name="Телефон")
        Favorite.objects.create(owner=self.owner, lot=lot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Favorite.objects.create(owner=self.owner, lot=lot)

    def test_initialize_does_not_reset_existing_password(self):
        with patch.dict("os.environ", {"DJANGO_SUPERUSER_USERNAME": "administrator", "DJANGO_SUPERUSER_PASSWORD": "different-password"}), patch("tender_search.core.management.commands.initialize.call_command"):
            call_command("initialize", stdout=StringIO())
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("test-strong-password"))


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class SearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user("search-owner", password="search-password")
        cls.other = get_user_model().objects.create_user("search-other", password="search-password")

    def specification(self, **kwargs):
        return {"terms": ["телефон", "связь"], "exclude": [], "mode": "any", "kind": "word", "pages": 1, "filters": {}, **kwargs}

    def job(self, **kwargs):
        return WebSearch.objects.create(owner=self.owner, specification=self.specification(**kwargs))

    def test_lot_back_link_preserves_origin_and_rejects_external_urls(self):
        self.client.force_login(self.owner)
        lot = store_lot(sample_lot())
        Lot.objects.filter(pk=lot.pk).update(refresh_state='done', checked_at=timezone.now())
        url = f'/lots/{lot.pk}/'
        for origin in (f'/search/{self.job().pk}/?page=2&view=cards', '/favorites/?sort=-amount&page=3'):
            response = self.client.get(url, {'next': origin})
            self.assertEqual(response.context['back_url'], origin)
            self.assertContains(response, '← Назад')
            self.assertEqual(self.client.get(url, HTTP_REFERER='http://testserver'+url).context['back_url'], origin)
        self.client.logout()
        self.client.force_login(self.owner)
        for unsafe in ('https://external.example/', 'http://testserver//external.example/'):
            self.assertEqual(self.client.get(url, {'next':unsafe}).context['back_url'], '/search/')
        self.assertEqual(self.client.get(url, HTTP_REFERER='http://testserver/favorites/?page=2').context['back_url'], '/favorites/?page=2')

    def test_search_deduplicates_and_cannot_be_delivered_twice(self):
        from unittest.mock import Mock
        api = Mock()
        api.pages.side_effect = lambda **kw: iter([Page(1, [sample_lot()], None)])
        job = self.job()
        execute_search(job.pk, api)
        execute_search(job.pk, api)
        job.refresh_from_db()
        self.assertEqual(job.hits.count(), 1)
        self.assertEqual(api.pages.call_count, 2)
        self.assertEqual(job.status, "done")
        self.assertTrue(job.complete)

    def test_same_procurement_under_different_source_ids_is_deduplicated(self):
        from unittest.mock import Mock
        api = Mock()
        row = {**sample_lot(), "customers": [{"id": 5, "bin": "123456789012", "name_ru": "Заказчик"}]}
        migrated = {**row, "id": 11, "announcement_id": 101}
        api.pages.side_effect = lambda **kw: iter([Page(2, [migrated, row], None)])
        job = self.job(terms=["телефон"])
        execute_search(job.pk, api)
        self.assertEqual(job.hits.count(), 1)

    def test_source_error_keeps_partial_results(self):
        from unittest.mock import Mock
        api = Mock()
        api.pages.side_effect = [iter([Page(2, [sample_lot()], "https://zakup.gov.kz/api/core/api/public/lots/?offset=50")]), SourceError("Источник недоступен")]
        job = self.job()
        execute_search(job.pk, api)
        job.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.assertEqual(job.error, "Источник недоступен")
        self.assertFalse(job.complete)
        self.assertEqual(job.hits.count(), 1)

    def test_source_error_is_distinct_from_empty_success(self):
        from unittest.mock import Mock
        api = Mock()
        api.pages.side_effect = SourceError("Ошибка API")
        job = self.job()
        execute_search(job.pk, api)
        job.refresh_from_db()
        self.assertEqual(job.status, "error")
        self.assertFalse(job.complete)

    def test_next_link_controls_completeness_even_if_count_is_small(self):
        from unittest.mock import Mock
        api = Mock()
        api.pages.side_effect = lambda **kw: iter([Page(1, [sample_lot()], "https://zakup.gov.kz/api/core/api/public/lots/?offset=50")])
        job = self.job()
        execute_search(job.pk, api)
        job.refresh_from_db()
        self.assertEqual(job.status, "partial")
        self.assertFalse(job.complete)

    def test_and_matches_are_within_one_lot(self):
        from unittest.mock import Mock
        api = Mock()
        rows = [sample_lot(1, "Телефон", ""), sample_lot(2, "Связь", "")]
        api.pages.side_effect = lambda **kw: iter([Page(2, rows, None)])
        job = self.job(mode="all")
        execute_search(job.pk, api)
        self.assertEqual(job.hits.count(), 0)

    def test_private_search_cannot_be_read_by_another_user(self):
        job = self.job()
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/search/{job.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/search/{job.pk}/", HTTP_HX_REQUEST="true").status_code, 404)

    def test_favorites_are_idempotent_and_private(self):
        lot = store_lot(sample_lot())
        self.client.force_login(self.owner)
        for _ in range(2):
            self.client.post(f"/lots/{lot.pk}/favorite/", {"action": "add"})
        self.assertEqual(Favorite.objects.filter(owner=self.owner).count(), 1)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get("/favorites/"), lot.number)
        self.client.post(f"/lots/{lot.pk}/favorite/", {"action": "remove"})
        self.assertEqual(Favorite.objects.filter(owner=self.owner).count(), 1)
        self.assertEqual(self.client.get(f"/lots/{lot.pk}/favorite/").status_code, 405)

    def test_form_validates_terms_and_ranges(self):
        form = SearchForm({"terms": "телефон; связь\nтелефон", "mode": "all", "kind": "word", "pages": "1", "number_kind": "lot_number"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.specification()["terms"], ["телефон", "связь"])
        invalid = SearchForm({"terms": "телефон", "mode": "any", "kind": "word", "pages": "1", "number_kind": "lot_number", "amount_min": "10", "amount_max": "1"})
        self.assertFalse(invalid.is_valid())

    def test_last_deadline_day_is_inclusive(self):
        form = SearchForm({"terms": "телефон", "mode": "any", "kind": "word", "pages": "1", "number_kind": "lot_number", "deadline_to": "2026-10-29"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.specification()["filters"]["offer_end_date__lte"].endswith("23:59:59+05:00"))

    def test_search_post_queues_job_and_renders_polling(self):
        self.client.force_login(self.owner)
        with patch("tender_search.core.views.tasks.web_search.delay") as delay:
            response = self.client.post("/search/", {"terms": "телефон", "mode": "any", "kind": "word", "pages": "1", "number_kind": "lot_number"})
        self.assertEqual(response.status_code, 302)
        delay.assert_called_once()
        self.assertContains(self.client.get(response.url), "hx-trigger=\"load delay:1000ms, app:resume\"")

    def test_list_snapshot_does_not_overwrite_verified_card(self):
        lot = store_lot(sample_lot())
        lot.checked_at, lot.status = timezone.now(), "Проверенный статус"
        lot.save()
        refreshed = store_lot(sample_lot())
        self.assertEqual(refreshed.status, "Проверенный статус")

    def test_refresh_failure_preserves_previous_verified_data(self):
        from unittest.mock import Mock
        api = Mock()
        api.announcement.side_effect = SourceError("Нет доступа")
        lot = store_lot(sample_lot())
        refresh_lot_data(lot.pk, api)
        lot.refresh_from_db()
        self.assertEqual(lot.name, "Телефон")
        self.assertEqual(lot.refresh_state, "error")
        self.assertIsNone(lot.checked_at)


@override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}, "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}})
class PersonalSearchSettingsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("settings-owner")
        self.other = get_user_model().objects.create_user("settings-other")
        self.client.force_login(self.owner)

    def test_saved_filters_roundtrip_and_owner_isolation(self):
        from .models import TelegramSubscription
        TelegramSubscription.objects.create(owner=self.owner, enabled=True, chat_id=self.owner.pk)
        data = {"terms": "телефон; смартфон", "exclude": "ремонт", "mode": "all", "kind": "word",
                "pages": "2", "number_kind": "lot_number", "amount_min": "5000", "action": "save", "profile_name": "Связь"}
        with patch("tender_search.core.tasks.web_search.delay") as publish:
            self.assertEqual(self.client.post("/search/", data).status_code, 302)
            publish.assert_not_called()
        profile = SearchProfile.objects.get(owner=self.owner)
        self.assertEqual(profile.name, "Связь")
        self.assertEqual(profile.filters["specification"]["filters"]["total_price__gte"], "5000")
        response = self.client.get(f"/search/?profile={profile.pk}")
        self.assertEqual(response.context["form"]["terms"].value(), data["terms"])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/search/?profile={profile.pk}").status_code, 404)
        self.assertEqual(self.client.post(f"/profiles/{profile.pk}/delete/").status_code, 404)
        self.assertNotContains(self.client.get("/search/"), "Связь")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/profiles/{profile.pk}/delete/").status_code, 405)
        self.assertEqual(self.client.post(f"/profiles/{profile.pk}/delete/").status_code, 302)
        self.assertFalse(SearchProfile.objects.filter(pk=profile.pk).exists())

    def test_result_view_is_personal_and_both_show_favorites(self):
        lot = store_lot(sample_lot())
        job = WebSearch.objects.create(owner=self.owner, specification={"terms": ["телефон"]}, status="done")
        SearchHit.objects.create(search=job, lot=lot, terms=["телефон"])
        url = f"/search/{job.pk}/"
        self.assertContains(self.client.get(url+"?view=cards"), 'class="lot-card"')
        self.assertContains(self.client.get(url), 'class="lot-card"')
        from bs4 import BeautifulSoup
        table_response = self.client.get(url+"?view=table")
        self.assertEqual(table_response.status_code, 200)
        self.assertIsNotNone(BeautifulSoup(table_response.content, 'html.parser').select_one('table'))
        self.assertContains(self.client.get(url), "В избранное")
        self.client.force_login(self.other)
        self.assertNotIn("result_view", self.client.session)


class WordTagsTests(TestCase):
    def test_words_are_split_and_count_is_not_limited(self):
        from .forms import split_terms
        words = [f"слово{i}" for i in range(1200)]
        self.assertEqual(split_terms(" ".join(words)), words)
        self.assertEqual(split_terms("телефон сотовой; связи,телефон\nноутбук"), ["телефон", "сотовой", "связи", "ноутбук"])
        form = SearchForm(data={"terms": " ".join(words), "mode": "any", "kind": "word", "pages": "1", "number_kind": "lot_number"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(len(form.specification()["terms"]), 1200)
        self.assertEqual(SearchForm()["mode"].value(), "any")


class DeliveryFilterTests(TestCase):
    def test_multiple_regions_city_and_saved_roundtrip(self):
        from .models import ReferenceEntry
        ReferenceEntry.objects.create(kind="katos", source_id=10, name="Область 1")
        ReferenceEntry.objects.create(kind="katos", source_id=20, name="Область 2")
        ReferenceEntry.objects.create(kind="cities", source_id=11, name="Город", raw={"region_id":10})
        data={"terms":"телефон", "mode":"any", "kind":"word", "pages":"1", "number_kind":"lot_number", "region":["10","20"], "city":["11"]}
        form=SearchForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.specification()["filters"]["delivery_address_id__in"], "11__20")
        user=get_user_model().objects.create_user("regions")
        self.client.force_login(user)
        from .models import TelegramSubscription
        TelegramSubscription.objects.create(owner=user, enabled=True, chat_id=user.pk)
        self.client.post("/search/", dict(data, action="save"))
        profile=SearchProfile.objects.get(owner=user)
        self.assertEqual(profile.filters["form"]["region"], ["10","20"])
        self.assertEqual(profile.filters["form"]["city"], ["11"])


class ProcurementCheckboxTests(TestCase):
    def test_defaults_multiple_values_and_explicit_empty(self):
        from .models import ReferenceEntry
        for kind, identifier, name in [("lot-statuses",6,"Опубликован"),("lot-statuses",9,"Приём заявок"),("purchase-methods",12,"Запрос ценовых предложений"),("purchase-methods",13,"Конкурс")]:
            ReferenceEntry.objects.create(kind=kind,source_id=identifier,name=name)
        form=SearchForm()
        self.assertEqual(form["status"].value(),["open:6","open:9"])
        self.assertEqual(form["method"].value(),["12"])
        legacy = SearchForm(initial={"status":"", "method":""})
        self.assertEqual(legacy["status"].value(),["open:6","open:9"])
        self.assertEqual(legacy["method"].value(),["12"])
        legacy_empty = SearchForm(initial={"status":[],"method":[]})
        self.assertEqual(legacy_empty["status"].value(),["open:6","open:9"])
        self.assertEqual(legacy_empty["method"].value(),["12"])
        data={"terms":"телефон","mode":"any","kind":"word","pages":"1","number_kind":"lot_number","status":["6","9"],"method":["12","13"]}
        form=SearchForm(data=data)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertEqual(form.specification()["filters"]["status_id__in"],"6__9")
        self.assertEqual(form.specification()["filters"]["purchase_method_id__in"],"12__13")
        self.assertEqual(SearchForm(initial={"status":[],"method":[],"_defaults_version":1})["status"].value(),[])


@override_settings(STORAGES={"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},"staticfiles":{"BACKEND":"django.contrib.staticfiles.storage.StaticFilesStorage"}})
class SavedResultExclusionTests(TestCase):
    def test_old_results_obey_word_part_exclusions(self):
        owner=get_user_model().objects.create_user("exclusion-owner")
        self.client.force_login(owner)
        job=WebSearch.objects.create(owner=owner,specification={"terms":["сайт"],"exclude":["услуг","ремонт"]},status="done")
        for identifier,name in [(101,"Услуги по информационной поддержке сайтов"),(102,"Ремонта сайта"),(103,"Разработка сайта")]:
            lot=store_lot(sample_lot(identifier=identifier,name=name,description=""))
            SearchHit.objects.create(search=job,lot=lot,terms=["сайт"])
        response=self.client.get(f"/search/{job.pk}/")
        self.assertEqual(response.context["page"].paginator.count,1)
        self.assertEqual(response.context["page"][0].lot.name,"Разработка сайта")


class SearchConditionsTests(TestCase):
    def test_conditions_show_actual_filter_names_and_values(self):
        from .models import ReferenceEntry
        from .templatetags.search_tags import search_conditions
        ReferenceEntry.objects.create(kind="lot-statuses",source_id=6,name="Опубликован")
        ReferenceEntry.objects.create(kind="purchase-methods",source_id=12,name="Запрос ценовых предложений")
        result=search_conditions({"terms":["сайт"],"exclude":["услуг"],"filters":{"status_id__in":"6","purchase_method_id__in":"12","total_price__gte":"1000","offer_end_date__lte":"2026-10-29T23:59:59+05:00"}})
        self.assertIn("сайт",result)
        self.assertIn("Статус: Опубликован",result)
        self.assertIn("Способ закупки: Запрос ценовых предложений",result)
        self.assertIn("Сумма от: 1000 ₸",result)
        self.assertIn("Окончание приёма до: 29.10.2026",result)
        self.assertNotIn("услуг",result)


@override_settings(STORAGES={"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},"staticfiles":{"BACKEND":"django.contrib.staticfiles.storage.StaticFilesStorage"}})
class ProfileSearchStateTests(TestCase):
    def test_search_keeps_saved_state_until_conditions_change(self):
        owner=get_user_model().objects.create_user("profile-state")
        self.client.force_login(owner)
        from .models import TelegramSubscription
        TelegramSubscription.objects.create(owner=owner, enabled=True, chat_id=owner.pk)
        data={"terms":"сайт","exclude":"ремонт","mode":"any","kind":"contains","pages":"1","number_kind":"lot_number"}
        self.client.post("/search/",dict(data,action="save"))
        profile=SearchProfile.objects.get(owner=owner)
        with patch("tender_search.core.tasks.web_search.delay"):
            response=self.client.post("/search/",dict(data,action="search",profile_id=profile.pk))
        result=self.client.get(response.url)
        self.assertTrue(result.context["profile_saved"])
        self.assertEqual(result.context["active_profile"],profile)
        with patch("tender_search.core.tasks.web_search.delay"):
            response=self.client.post("/search/",dict(data,action="search",profile_id=profile.pk,exclude="монтаж"))
        self.assertFalse(self.client.get(response.url).context["profile_saved"])


class AutoPaginationTests(TestCase):
    def test_new_search_traverses_multiple_pages(self):
        from unittest.mock import Mock
        owner=get_user_model().objects.create_user("auto-pages")
        job=WebSearch.objects.create(owner=owner,specification={"terms":["телефон"],"filters":{},"pages":100,"auto_pages":True})
        api=Mock()
        api.pages.return_value=iter([Page(2,[sample_lot(901)],"https://zakup.gov.kz/api/core/api/public/lots/?offset=100"),Page(2,[sample_lot(902)],None)])
        execute_search(job.pk,api)
        job.refresh_from_db()
        api.pages.assert_called_once_with(q="телефон",limit=100,ordering="-publish_date",max_pages=100)
        self.assertTrue(job.complete)
        self.assertEqual(job.hits.count(),2)
        self.assertEqual(job.scanned,2)


@override_settings(STORAGES={"default":{"BACKEND":"django.core.files.storage.FileSystemStorage"},"staticfiles":{"BACKEND":"django.contrib.staticfiles.storage.StaticFilesStorage"}})
class PublicationOrderingTests(TestCase):
    def test_publication_date_precedes_source_id(self):
        owner=get_user_model().objects.create_user("date-order")
        self.client.force_login(owner)
        job=WebSearch.objects.create(owner=owner,specification={"terms":["телефон"]},status="done")
        for identifier,date in [(100,"2026-10-28T10:00:00+00:00"),(200,"2026-10-27T10:00:00+00:00")]:
            lot=store_lot(dict(sample_lot(identifier),announcement_publish_date=date))
            SearchHit.objects.create(search=job,lot=lot,terms=["телефон"])
        response=self.client.get(f"/search/{job.pk}/")
        self.assertEqual([hit.lot.source_id for hit in response.context["page"]],[100,200])
