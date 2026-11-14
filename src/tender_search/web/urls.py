from tender_search.core import collection_views
from tender_search.core import draft_queue, shipping
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path, re_path
from django.http import HttpResponseForbidden
from django.views.generic import RedirectView, TemplateView

from tender_search.core.views import dashboard, health
from tender_search.core import views
from tender_search.core import lead_views
from tender_search.core import user_admin
from tender_search.core import public_views
from tender_search.core import seo
from tender_search.core import guide_views
from tender_search.core import catalog_views
from tender_search.core import auth_views as account_views
from tender_search.core.extension_views import download_extension, receive_extension_report
from tender_search.core.preparation import prepare_application, export_preparation, import_portal_status

urlpatterns = [
    path('account/identity/', views.account_identity, name='account_identity'),
    path('lots/<int:lot_id>/shipment/', shipping.save, name='shipment_save'),
    path('lots/<int:lot_id>/shipment/act/', shipping.upload_act, name='shipment_act'),
    path('lots/<int:lot_id>/shipment/act/download/', shipping.download_act, name='shipment_act_download'),
    path('workspace/drafts/', draft_queue.queue, name='draft_queue'),
    path('workspace/drafts/add/', draft_queue.enqueue, name='draft_queue_add'),
    path('workspace/drafts/<int:item_id>/restart/', draft_queue.restart, name='draft_queue_restart'),
    path('workspace/drafts/<int:item_id>/check/', draft_queue.check_portal, name='draft_queue_check'),
    path('workspace/drafts/<int:item_id>/packet/', draft_queue.packet, name='draft_queue_packet'),
    path('workspace/drafts/<int:item_id>/guard/', draft_queue.launch_guard, name='draft_queue_guard'),
    path('workspace/drafts/<int:item_id>/progress/', draft_queue.progress, name='draft_queue_progress'),
    path('workspace/drafts/<int:item_id>/cancel/', draft_queue.cancel, name='draft_queue_cancel'),
    path('settings/extension/report/', receive_extension_report, name='receive_extension_report'),
    path("", public_views.landing, name="landing"),
    path("robots.txt", seo.robots, name="robots"),
    path("sitemap.xml", seo.sitemap, name="sitemap"),
    path("guides/", guide_views.guides, name="guides"),
    path("guides/<slug:slug>/", guide_views.guide, name="guide"),
    path("catalog/", catalog_views.catalog, name="catalog"),
    path("catalog/categories/<slug:category_slug>/", catalog_views.catalog, name="catalog_category"),
    path("catalog/regions/<slug:region_slug>/", catalog_views.catalog, name="catalog_region"),
    path("catalog/lots/<int:source_id>/", catalog_views.catalog_lot, name="catalog_lot"),
    path("register/", public_views.register, name="register"),
    path("account/access/", public_views.account_access, name="account_access"),
    path("administration/users/<int:user_id>/access/", public_views.edit_access, name="administration_user_access"),
    path("main/", dashboard, name="dashboard"),
    path("login/", account_views.ProtectedLoginView.as_view(), name="login"),
    path('password/reset/', account_views.password_reset, name='password_reset'),
    path('password/reset/sent/', TemplateView.as_view(template_name='public/auth_notice.html', extra_context={
        'notice_title': 'Проверьте почту',
        'notice_text': 'Если для этого email есть активный аккаунт с паролем, мы отправим ссылку восстановления. Проверьте также папку «Спам». Ссылка действует 1 час.',
    }), name='password_reset_done'),
    path('password/reset/<uidb64>/<token>/', account_views.ProtectedPasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('password/complete/', TemplateView.as_view(template_name='public/auth_notice.html', extra_context={
        'notice_title': 'Пароль изменён', 'notice_text': 'Теперь войдите в аккаунт с новым паролем. Остальные сеансы завершены.',
    }), name='password_reset_complete'),
    path('account/email/', account_views.email_notice, name='email_notice'),
    path('account/email/resend/', account_views.email_resend, name='email_resend'),
    path('account/email/confirm/<str:token>/', account_views.email_confirm, name='email_confirm'),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    re_path(r"^admin(?:/.*)?$", lambda request: HttpResponseForbidden("Администрирование временно отключено."), name="admin_disabled"),
    path("health/", health, name="health"),
    path("search/", views.search, name="search"),
    path("search/index-status/", views.index_status, name="index_status"),
    path("profiles/<int:profile_id>/rename/", views.profile_rename, name="profile_rename"),
    path("profiles/<int:profile_id>/delete/", views.profile_delete, name="profile_delete"),
    path("search/<uuid:job_id>/cancel/", views.search_cancel, name="search_cancel"),
    path("search/<uuid:job_id>/", views.search_results, name="search_results"),
    path("lots/ui-state/", views.lot_ui_state, name="lot_ui_state"),
    path("lots/<int:lot_id>/", views.lot_detail, name="lot_detail"),
    path("lots/<int:lot_id>/preparation/", prepare_application, name="application_preparation"),
    path("lots/<int:lot_id>/preparation/export/", export_preparation, name="preparation_export"),
    path("lots/<int:lot_id>/preparation/status/", import_portal_status, name="preparation_status"),
    path("lots/<int:lot_id>/specification/", views.lot_specification_extract, name="lot_specification_extract"),
    path("lots/<int:lot_id>/status/", views.lot_detail_status, name="lot_detail_status"),
    path("lots/<int:lot_id>/price/", views.lot_price_save, name="lot_price_save"),
    path("lots/<int:lot_id>/note/", views.lot_note_save, name="lot_note_save"),
    path("lots/<int:lot_id>/refresh/", views.lot_refresh, name="lot_refresh"),
    path("lots/<int:lot_id>/favorite/", views.favorite, name="favorite"),
    path("settings/", views.notification_settings, name="notification_settings"),
    path("settings/extension/download/", download_extension, name="download_extension"),
    path("administration/", user_admin.users, name="administration_users"),
    path("administration/users/<int:user_id>/edit/", user_admin.edit_user, name="administration_user_edit"),
    path("administration/users/<int:user_id>/delete/", user_admin.delete_user, name="administration_user_delete"),
    path("administration/clients/", lead_views.leads, name="administration_leads"),
    path("administration/collection-status/", lead_views.collection_status, name="lead_collection_status"),
    path("administration/clients/export/", lead_views.export_leads, name="administration_leads_export"),
    path("administration/clients/<int:company_id>/", lead_views.lead_detail, name="administration_lead_detail"),
    path("profiles/<int:profile_id>/notifications/", views.profile_notifications, name="profile_notifications"),
    path("lots/<int:lot_id>/status-watch/", views.status_watch, name="status_watch"),
    path("lots/<int:lot_id>/documents/retry/", views.document_retry, name="document_retry"),
    path("lots/<int:lot_id>/documents/<int:version_id>/", views.document_download, name="document_download"),
    path("workspace/", collection_views.collections, name="workspace"),
    path("workspace/lots/<int:lot_id>/action/", collection_views.fulfillment_action, name="workspace_action"),
    path("profiles/<int:profile_id>/tracking/", collection_views.tracking, name="profile_tracking"),
    path("profiles/<int:profile_id>/refresh-collection/", collection_views.refresh, name="collection_refresh"),
    path("workspace/items/<int:item_id>/stage/", collection_views.move, name="collection_move"),
    path("favorites/", views.favorites, name="favorites"),
]
