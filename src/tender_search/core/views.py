from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction, IntegrityError
from django.db.models import DateTimeField, F, Q, Value, OuterRef, Subquery
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, NullIf, Coalesce
from django.http import JsonResponse, FileResponse, Http404
from django.shortcuts import render
from django.shortcuts import get_object_or_404, redirect
from django.core.paginator import Paginator
from django.views.decorators.http import require_POST
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from datetime import timedelta
from urllib.parse import urlsplit, urlencode
from django.urls import reverse
from hashlib import sha256

from .models import Favorite, Notification, SearchProfile, WebSearch, Lot, TelegramSubscription, TelegramDelivery, AnnouncementWatch, StatusDelivery, Document, DocumentVersion, LotDocumentDownload, UserLotNote, UserLotPrice, CompanySettings, LotOutcomeWatch, LotSpecification
from .forms import SearchForm
from . import tasks
from tender_search.matching import matches
from tender_search.client import utc_datetime, SchemaError


@login_required
def index_status(request):
    from .user_admin import can_manage_users
    if not can_manage_users(request.user):
        raise PermissionDenied
    return render(request, 'core/_index_coverage.html')


def account_identity(request):
    from django.utils.cache import patch_vary_headers
    response = JsonResponse({'user_id': str(request.user.pk) if request.user.is_authenticated else None})
    response['Cache-Control'] = 'no-store, private'
    patch_vary_headers(response, ['Cookie'])
    return response


@login_required
def dashboard(request):
    return render(request, "core/dashboard.html", {
        "profiles": SearchProfile.objects.filter(owner=request.user).count(),
        "tracked": SearchProfile.objects.filter(owner=request.user, collection_enabled=True).count(),
        "favorites": Favorite.objects.filter(owner=request.user).count(),
        "favorites_tracked": Favorite.objects.filter(owner=request.user,lot__announcement_id__in=AnnouncementWatch.objects.filter(owner=request.user,enabled=True).values("announcement_id")).count(),
        "notifications": Notification.objects.filter(owner=request.user, read_at__isnull=True).count(),
    })


@login_required
def search(request):
    initial = None
    profile = None
    if request.GET.get("profile"):
        profile = get_object_or_404(SearchProfile, pk=request.GET["profile"], owner=request.user)
        initial = profile.filters.get("form", {})
    form = SearchForm(request.POST if request.method == "POST" else None, initial=initial)
    if request.method == "POST" and request.POST.get("action") == "save":
        if not TelegramSubscription.objects.filter(owner=request.user, enabled=True, chat_id__isnull=False).exists():
            form.is_valid()
            form.add_error(None, "Для сохранения подборки подключите Telegram в настройках.")
    if request.method == "POST" and form.is_valid():
        if request.POST.get("action") == "save":
            with transaction.atomic():
                values = {key: (request.POST.getlist(key) if key in {"region", "city", "status", "method"} else request.POST.get(key, "")) for key in form.fields}
                values["_defaults_version"] = 1
                values["kind"] = form.cleaned_data["kind"]
                values["terms"] = "; ".join(form.cleaned_data["terms"])
                name = request.POST.get("profile_name", "").strip()[:200] or " · ".join(form.cleaned_data["terms"])[:200]
                settings = dict(name=name, include_terms=form.cleaned_data["terms"], exclude_terms=form.cleaned_data["exclude"],
                    mode=form.cleaned_data["mode"], enabled=True, collection_enabled=True,
                    next_monitor_at=None, collection_next_at=None, collection_error='',
                    filters={"form": values, "specification": form.specification()})
                if request.POST.get("profile_id"):
                    profile = get_object_or_404(SearchProfile.objects.select_for_update(), pk=request.POST["profile_id"], owner=request.user)
                    if not request.POST.get("profile_name", "").strip():
                        settings["name"] = profile.name
                    if profile.filters != settings["filters"]:
                        profile.collection_revision += 1
                        profile.collection_enabled = True
                        profile.collection_next_at = None
                        profile.collection_lease = ""
                        profile.collection_lease_until = None
                        from .models import CollectionLot
                        CollectionLot.objects.filter(profile=profile).update(matches_filter=False)
                        profile.baseline_at = None
                        profile.index_baseline_at = None
                        profile.next_monitor_at = None
                        profile.monitor_lease = ""
                        profile.monitor_lease_until = None
                        TelegramDelivery.objects.filter(profile=profile,state="pending").update(state="cancelled")
                    for key, value in settings.items():
                        setattr(profile, key, value)
                    profile.save()
                else:
                    profile = SearchProfile.objects.create(owner=request.user, **settings)
                from .collections_service import enqueue_collection
                transaction.on_commit(lambda: enqueue_collection(profile.pk))
                return redirect(f"{request.path}?profile={profile.pk}")
        if WebSearch.objects.filter(owner=request.user, status__in=["queued", "running"],
                                    created_at__gte=timezone.now()-timedelta(minutes=25)).count() >= 2:
            form.add_error(None, "У вас уже выполняются два поиска. Дождитесь завершения одного из них.")
        else:
            specification = form.specification()
            if request.POST.get("profile_id"):
                selected_profile = get_object_or_404(SearchProfile, pk=request.POST["profile_id"], owner=request.user)
                specification["profile_id"] = selected_profile.pk
            job = WebSearch.objects.create(owner=request.user, specification=specification)
            try:
                tasks.web_search.delay(str(job.pk))
            except Exception:
                job.status, job.error = "error", "Очередь поиска недоступна. Повторите попытку позже."
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error", "finished_at"])
            return redirect(f"{reverse('search_results', kwargs={'job_id': job.pk})}#search-results")
    return render(request, "core/search.html", {"form": form, "active_profile": profile, "profile_saved": bool(profile) and not form.is_bound, "profiles": SearchProfile.objects.filter(owner=request.user).order_by("-updated_at"), "recent": WebSearch.objects.filter(owner=request.user, specification__monitor_profile_id__isnull=True, specification__collection_profile_id__isnull=True).order_by("-created_at")[:7]})


@login_required
@require_POST
def profile_rename(request, profile_id):
    from .forms import ProfileNameForm
    profile = get_object_or_404(SearchProfile, pk=profile_id, owner=request.user)
    form = ProfileNameForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'error': form.errors['name'][0]}, status=400)
    name = form.cleaned_data['name']
    # Rename independently of filters and monitor leases, including concurrent refreshes.
    SearchProfile.objects.filter(pk=profile.pk, owner=request.user).update(name=name, updated_at=timezone.now())
    return JsonResponse({'name': name})


@login_required
@require_POST
def profile_delete(request, profile_id):
    from .workflow_state import delete_profile_preserving_work
    from django.contrib import messages
    get_object_or_404(SearchProfile, pk=profile_id, owner=request.user)
    try:
        delete_profile_preserving_work(request.user.pk, profile_id)
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("search")


def result_context(request, lots):
    from .lot_state import apply_personal_state
    apply_personal_state(request.user, lots)
    for lot in lots:
        try:
            lot.offer_start = utc_datetime(lot.raw.get("offer_start_date"))
        except (ValueError, TypeError, SchemaError):
            lot.offer_start = None


@login_required
def lot_ui_state(request):
    from .lot_state import apply_personal_controls, collection_lot_count
    raw_ids = request.GET.get('ids', '').split(',')
    if len(raw_ids) > 100 or any(not value.isdecimal() or len(value) > 18 for value in raw_ids):
        return JsonResponse({'error': 'Invalid lot ids'}, status=400)
    lots = list(Lot.objects.filter(pk__in=[int(value) for value in raw_ids]).only('pk', 'announcement_id'))
    apply_personal_controls(request.user, lots)
    response = JsonResponse({'owner': str(request.user.pk), 'lots': [
        {'id': str(lot.pk), 'favorite': lot.is_favorite, 'watched': lot.is_status_watched,
         'saved': lot.is_in_collection} for lot in lots],
        'favorite_count': Favorite.objects.filter(owner=request.user).count(),
        'locked_lots': [str(lot.pk) for lot in lots if lot.favorite_locked],
        'collection_count': collection_lot_count(request.user)})
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
@require_POST
def search_cancel(request, job_id):
    job = get_object_or_404(WebSearch, pk=job_id, owner=request.user)
    WebSearch.objects.filter(pk=job.pk, status__in=['queued', 'running']).update(
        status='cancelled', complete=False, error='', finished_at=timezone.now(), updated_at=timezone.now())
    return redirect('search_results', job_id=job.pk)


@login_required
def search_results(request, job_id):
    from .pagination import page_settings
    pagination=page_settings(request,'search')
    if request.GET.get("view") in {"table", "cards"}:
        request.session["result_view"] = request.GET["view"]
    job = get_object_or_404(WebSearch, pk=job_id, owner=request.user)
    if job.status in {"queued", "running"} and job.updated_at < timezone.now()-timedelta(minutes=25):
        job.status = "partial" if job.hits.exists() else "error"
        job.error = "Поиск не завершился вовремя. Сохранённые результаты доступны; можно запустить новый поиск."
        job.complete = False
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error", "complete", "finished_at"])
    selected_sort = request.GET.get('sort', '-published')
    sort_fields = {'amount': 'lot__amount', 'start': 'offer_start_sort', 'end': 'lot__announcement__deadline'}
    sort_key = selected_sort.lstrip('-')
    if selected_sort not in {prefix+key for prefix in ('', '-') for key in sort_fields}:
        selected_sort, sort_key = '-published', 'published'
    descending = selected_sort.startswith('-')
    hits = job.hits.select_related("lot__announcement", "lot__customer").annotate(
        publication=Cast(KeyTextTransform("announcement_publish_date", "lot__raw"), DateTimeField())
    )
    order = [F('publication').desc(nulls_last=True), '-lot__source_id', '-pk']
    if sort_key == 'start':
        hits = hits.annotate(offer_start_sort=Cast(
            NullIf(KeyTextTransform('offer_start_date', 'lot__raw'), Value('')), DateTimeField()))
    if sort_key in sort_fields:
        field = F(sort_fields[sort_key])
        order.insert(0, field.desc(nulls_last=True) if descending else field.asc(nulls_last=True))
    hits = hits.order_by(*order)
    if job.status in {'queued', 'running'}:
        # Intermediate hits are hidden until completion; avoid loading their
        # rows and related objects on every readiness poll.
        hits = hits.none()
    sort_headers = {}
    for key, label in [('amount', 'Сумма, ₸'), ('start', 'Начало'), ('end', 'Окончание')]:
        active = sort_key == key
        next_sort = ('-' if active and not descending else '') + key
        url = f'?sort={next_sort}&per_page={pagination["per_page"]}'
        sort_headers[key] = {'label': label, 'url': url,
            'arrow': ('↓' if descending else '↑') if active else '↕',
            'aria': ('descending' if descending else 'ascending') if active else 'none'}
    labels = {'amount': 'По сумме', 'start': 'По началу подачи', 'end': 'По окончанию подачи'}
    age = max(0, (timezone.now() - job.created_at).total_seconds())
    sort_context = {**pagination,'poll_delay': 1000 if age < 5 else 2000 if age < 20 else 5000,
        'result_sort': selected_sort if sort_key != 'published' else '',
        'result_sort_label': (labels[sort_key] + (' ↓' if descending else ' ↑')) if sort_key in labels else 'Сначала новые',
        'sort_headers': sort_headers}
    excluded_terms = job.specification.get("exclude", [])
    if excluded_terms:
        excluded_ids = [identifier for identifier, name, description in hits.values_list("pk", "lot__name", "lot__description").iterator()
                        if any(matches(f"{name}\n{description}", term, "substring") for term in excluded_terms)]
        hits = hits.exclude(pk__in=excluded_ids)
    page = Paginator(hits, pagination['per_page']).get_page(request.GET.get("page"))
    result_context(request, [hit.lot for hit in page])
    if request.headers.get("HX-Request"):
        # Filtering, pagination and polling only replace the results block.
        # Building SearchForm here would reload large location dictionaries.
        return render(request, "core/_results.html", {
            **sort_context,
            "job": job, "local_coverage": job.source_counts.get("__index__"),
            "page": page, "pending": job.status in {"queued", "running"},
            })
    spec = job.specification
    initial = {"terms": "; ".join(spec.get("terms", [])), "exclude": "; ".join(spec.get("exclude", [])),
               "mode": spec.get("mode", "any"), "kind": spec.get("kind", "contains"), "pages": spec.get("pages", 1), "depth": spec.get("depth", 500),
               "customer_bin": spec.get("customer_bin", ""),
               "only_advance": spec.get("filters", {}).get("only_advance", False),
               "only_medical": spec.get("filters", {}).get("only_medical", False),
               "in_documents": spec.get("filters", {}).get("in_documents", False),
               "remaining_hours": str(spec.get("filters", {}).get("remaining_hours", "")),
               "subject": spec.get("filters", {}).get("subject", "")}
    for field, param in [("amount_min", "total_price__gte"), ("amount_max", "total_price__lte"),
                         ("deadline_from", "offer_end_date__gte"), ("deadline_to", "offer_end_date__lte"),
                         ("status", "status_id"), ("method", "purchase_method_id"), ("region", "delivery_address_id__in")]:
        value = spec.get("filters", {}).get(param, "")
        initial[field] = value[:10] if field.startswith("deadline") else value
    for field, param in [("status", "status_id"), ("method", "purchase_method_id")]:
        value = spec.get("filters", {}).get(param + "__in", spec.get("filters", {}).get(param, ""))
        initial[field] = str(value).split("__") if value else []
    if "status_selection" in spec:
        initial["status"] = spec["status_selection"]
    initial["_defaults_version"] = spec.get("defaults_version", 0)
    if "regions" in spec:
        initial["region"] = spec["regions"]
        initial["city"] = spec.get("cities", [])
    for number_kind in ["lot_number", "announcement_number"]:
        if number_kind in spec.get("filters", {}):
            initial.update(number=spec["filters"][number_kind], number_kind=number_kind)
    active_profile = SearchProfile.objects.filter(pk=spec.get("profile_id"), owner=request.user).first() if spec.get("profile_id") else None
    profile_saved = False
    if active_profile:
        current_spec = {key: value for key, value in spec.items() if key != "profile_id"}
        ignored = {'depth', 'pages', 'auto_pages', 'backend'}
        profile_saved = {k:v for k,v in current_spec.items() if k not in ignored} == {k:v for k,v in active_profile.filters.get('specification', {}).items() if k not in ignored}
    return render(request, "core/results.html", {**sort_context, "active_profile": active_profile, "profile_saved": profile_saved, "job": job, "local_coverage": job.source_counts.get("__index__"), "page": page, "pending": job.status in {"queued", "running"},
         "form": SearchForm(initial=initial), "profiles": SearchProfile.objects.filter(owner=request.user).order_by("-updated_at")})


def queue_refresh(lot):
    now = timezone.now()
    if lot.refresh_started_at and lot.refresh_started_at > now-timedelta(seconds=30):
        return
    updated = Lot.objects.filter(pk=lot.pk).exclude(refresh_state="running").update(
        refresh_state="running", refresh_started_at=now, refresh_error="")
    if updated:
        try:
            tasks.refresh_lot.delay(lot.pk)
        except Exception:
            Lot.objects.filter(pk=lot.pk).update(refresh_state="error", refresh_error="Очередь обновления недоступна.")


def lot_revisions(lot, batch, specification):
    def revision(*values):
        return sha256(repr(values).encode()).hexdigest()[:20]
    return {
        'detail': revision(lot.refresh_state, lot.checked_at, lot.refresh_error),
        'documents': revision(batch.state, batch.updated_at, batch.error) if batch else 'none',
        'specification': revision(specification.state, specification.updated_at, specification.error) if specification else 'none',
    }


def expire_lot_jobs(lot, batch=None, specification=None):
    now = timezone.now()
    if lot.refresh_state == 'running' and lot.refresh_started_at and lot.refresh_started_at < now-timedelta(minutes=6):
        updated = Lot.objects.filter(pk=lot.pk, refresh_state='running', refresh_started_at=lot.refresh_started_at).update(refresh_state='error', refresh_error='Обновление не завершилось вовремя.')
        if updated:
            # Keep the revision paired with the loaded body even if the worker
            # finishes immediately after this timeout update.
            lot.refresh_state = 'error'
            lot.refresh_error = 'Обновление не завершилось вовремя.'
    for job, minutes, message in (
        (batch, 25, 'Загрузка не завершилась вовремя. Повторите попытку.'),
        (specification, 15, 'Извлечение не завершилось вовремя. Повторите запрос.'),
    ):
        if job and job.state in ('queued', 'running') and job.updated_at < now-timedelta(minutes=minutes):
            type(job).objects.filter(pk=job.pk, state=job.state, updated_at=job.updated_at).update(state='error', error=message, updated_at=now)
            job.refresh_from_db(fields=['state', 'error', 'updated_at'])


@login_required
def lot_detail_status(request, lot_id):
    # Poll metadata only; never reload large document texts or enqueue a new refresh.
    lot = get_object_or_404(Lot.objects.only('refresh_state', 'refresh_error', 'refresh_started_at', 'checked_at'), pk=lot_id)
    favorite = Favorite.objects.filter(owner=request.user, lot_id=lot_id).exists()
    batch = LotDocumentDownload.objects.filter(lot_id=lot_id).first() if favorite else None
    specification = LotSpecification.objects.filter(owner=request.user, lot_id=lot_id).defer('documents').first()
    expire_lot_jobs(lot, batch, specification)
    revisions = lot_revisions(lot, batch, specification)
    pending = lot.refresh_state == 'running' or any(job and job.state in ('queued', 'running') for job in (batch, specification))
    data = {'revisions': revisions, 'pending': bool(pending), 'owner': request.user.pk}
    if any(request.GET.get(key) != value for key, value in revisions.items()):
        data['html'] = lot_detail(request, lot_id, updates=True).content.decode()
    response = JsonResponse(data)
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def lot_detail(request, lot_id, price_form=None, updates=False):
    lot = get_object_or_404(Lot.objects.select_related("announcement", "customer"), pk=lot_id)
    loaded_checked_at = lot.checked_at
    back_key = f"lot_return_{lot_id}"
    back_url = reverse('search')
    candidates = (request.session.get(back_key),) if updates else (request.GET.get('next'), request.META.get('HTTP_REFERER'), request.session.get(back_key))
    for candidate in candidates:
        if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
            parts = urlsplit(candidate)
            if not parts.path.startswith('/') or parts.path.startswith('//') or parts.path == request.path:
                continue
            back_path = reverse('workspace') if parts.path == '/collections/' else parts.path
            back_url = back_path + ('?' + parts.query if parts.query else '')
            if not updates and request.session.get(back_key) != back_url:
                request.session[back_key] = back_url
            break
    result_context(request, [lot])
    expire_lot_jobs(lot)
    if not updates and not lot.is_archived and (not lot.refresh_state or (lot.refresh_state == "done" and lot.checked_at and lot.checked_at < timezone.now()-timedelta(minutes=5))):
        queue_refresh(lot)
        # The task updates the body asynchronously. Keep joined relations and
        # request-scoped pricing data; only refresh its scheduling metadata now.
        lot.refresh_from_db(fields=['refresh_state', 'refresh_started_at', 'refresh_error', 'checked_at'])
    if lot.checked_at != loaded_checked_at:
        # A fast worker may already have committed new data. Read body and
        # revision together, then rebuild quantities/prices from that same body.
        lot = get_object_or_404(Lot.objects.select_related('announcement', 'customer'), pk=lot_id)
        result_context(request, [lot])
    batch = LotDocumentDownload.objects.filter(lot=lot).first() if lot.is_favorite else None
    documents = []
    if lot.is_favorite or lot.is_archived:
        latest = DocumentVersion.objects.filter(document_id=OuterRef('pk')).order_by('-pk').values('pk')[:1]
        for doc in Document.objects.filter(announcement=lot.announcement).filter(Q(lot=lot)|Q(lot__isnull=True)).annotate(saved_version_id=Coalesce(F('current_version_id'), Subquery(latest))).order_by('name'):
            documents.append(doc)
    specification = LotSpecification.objects.filter(owner=request.user,lot=lot).first()
    expire_lot_jobs(lot, batch, specification)
    from .lot_pricing import pricing_context
    from .shipping import context as shipment_context
    from .models import CollectionLot
    untriaged_item = CollectionLot.objects.filter(profile__owner=request.user,lot=lot,stage='new').order_by('pk').first() if lot.workflow and lot.workflow.stage == 'new' else None
    return render(request, "core/_lot_detail.html" if updates or request.headers.get("HX-Request") else "core/lot_detail.html", {"untriaged_item":untriaged_item,"lot_description": "\n".join(dict.fromkeys(line.strip() for line in lot.description.splitlines())), "lot_revisions":lot_revisions(lot,batch,specification),"specification":specification,"specification_pending":bool(specification and specification.state in ('queued','running')),**pricing_context(request.user, lot, price_form), **shipment_context(request.user, lot), "lot": lot, "back_url": back_url, "documents": documents, "document_batch": batch, "documents_pending": bool(batch and batch.state in {"queued", "running"}), "outcome_watch": lot.personal_outcome_watch, "lot_note": UserLotNote.objects.filter(owner=request.user, lot=lot).values_list("text", flat=True).first() or ""})


@login_required
@require_POST
def lot_specification_extract(request, lot_id):
    from .specification_service import queue_specification
    lot=get_object_or_404(Lot,pk=lot_id)
    specification=queue_specification(request.user,lot,refresh=request.POST.get('refresh')=='1')
    if request.headers.get('X-Lot-Async') == '1':
        from django.template.loader import render_to_string
        pending=specification.state in ('queued','running')
        response=JsonResponse({'queued':pending,'specification_html':render_to_string('core/_lot_specification.html',{
            'lot':lot,'specification':specification,'specification_pending':pending},request=request)})
        response['Cache-Control']='private, no-store'
        return response
    target=reverse('lot_detail',kwargs={'lot_id':lot.pk})
    back=request.POST.get('next','')
    if back and url_has_allowed_host_and_scheme(back,allowed_hosts={request.get_host()},require_https=request.is_secure()):
        target+='?'+urlencode({'next':back})
    return redirect(target+'#lot-specification')


@login_required
@require_POST
def lot_price_save(request, lot_id):
    from .lot_pricing import price_entry_data, LotPriceForm, company_vat_rate
    from .models import CollectionLot, DraftQueueItem
    from .outcome_service import register_watch
    from django.contrib import messages
    lot = get_object_or_404(Lot.objects.select_related('announcement'), pk=lot_id)
    if DraftQueueItem.objects.filter(owner=request.user,lot=lot,state__in=('running','waiting')).exists():
        raise PermissionDenied
    if price_entry_data(lot) is None:
        raise PermissionDenied
    if not CollectionLot.objects.filter(profile__owner=request.user,lot=lot,stage='preparing').exists() or UserLotPrice.objects.filter(owner=request.user,lot=lot,locked_at__isnull=False).exists():
        raise PermissionDenied
    form = LotPriceForm(request.POST, lot=lot, initial={'vat_rate': company_vat_rate(request.user)})
    if not form.is_valid():
        from .preparation import prepare_application
        response = prepare_application(request, lot_id, price_form=form)
        response.status_code = 400
        return response
    with transaction.atomic():
        list(SearchProfile.objects.select_for_update().filter(owner=request.user).order_by('pk'))
        interested=list(CollectionLot.objects.select_for_update().filter(profile__owner=request.user,lot=lot,stage='preparing'))
        saved=UserLotPrice.objects.select_for_update().filter(owner=request.user,lot=lot).first()
        if not interested or (saved and saved.locked_at):
            raise PermissionDenied
        UserLotPrice.objects.update_or_create(owner=request.user,lot=lot,
            defaults={'unit_gross':form.cleaned_data['unit_gross'],'vat_rate':form.cleaned_data['vat_rate'], 'locked_at':None})
        messages.success(request, 'Цена сохранена в подготовке. Заявка ещё не подана.')
    target = reverse('application_preparation', kwargs={'lot_id':lot.pk})
    back = request.POST.get('next', '')
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target += '?' + urlencode({'next':back})
    return redirect(target)


@login_required
@require_POST
def lot_note_save(request, lot_id):
    from django.contrib import messages
    lot = get_object_or_404(Lot, pk=lot_id)
    text = request.POST.get('note', '')
    if len(text) > 10000:
        messages.error(request, 'Заметка должна содержать не более 10 000 символов.')
    else:
        UserLotNote.objects.update_or_create(owner=request.user, lot=lot, defaults={'text': text})
        messages.success(request, 'Заметка сохранена.')
    return_to = request.POST.get('return_to', '')
    if (return_to and url_has_allowed_host_and_scheme(return_to, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            and urlsplit(return_to).path == reverse('workspace')):
        return redirect(return_to)
    target = reverse('lot_detail', kwargs={'lot_id': lot.pk})
    back = request.POST.get('next', '')
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target += '?' + urlencode({'next': back})
    return redirect(target)


@login_required
@require_POST
def lot_refresh(request, lot_id):
    lot = get_object_or_404(Lot, pk=lot_id)
    queue_refresh(lot)
    if request.headers.get('X-Lot-Async') == '1':
        return JsonResponse({'queued': True})
    return redirect("lot_detail", lot_id=lot.pk)


@login_required
@require_POST
def favorite(request, lot_id):
    lot = get_object_or_404(Lot, pk=lot_id)
    from .workflow_state import add_to_interesting, remove_from_work
    error = ''
    if request.POST.get('action') == 'remove':
        if not remove_from_work(request.user.pk,lot.pk):
            error = 'Лот уже в работе. Снять звезду можно после явного завершения или отказа.'
    elif request.POST.get('action') == 'add':
        add_to_interesting(request.user.pk,lot.pk)
    if request.headers.get("HX-Request"):
        result_context(request, [lot])
        return render(request, "core/_lot_actions.html", {"lot": lot, "update_favorite_count": True, "watch_error": error, "return_to": request.POST.get("next", "")})
    if error:
        from django.contrib import messages
        messages.error(request, error)
    next_url = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(next_url)
    return redirect("lot_detail", lot_id=lot.pk)


@login_required
def favorites(request):
    ordering = {'amount': 'amount', 'added': 'favorite__created_at', 'status': 'status'}
    selected = request.GET.get('sort', '-added')
    key = selected.lstrip('-')
    if key not in ordering:
        selected, key = '-added', 'added'
    descending = selected.startswith('-')
    field = ordering[key]
    order = F(field).desc(nulls_last=True) if descending else F(field).asc(nulls_last=True)
    lots = Lot.objects.filter(favorite__owner=request.user).select_related('announcement', 'customer').annotate(
        favorite_added=F('favorite__created_at')).order_by(order, '-pk')
    page = Paginator(lots, 25).get_page(request.GET.get('page'))
    result_context(request, list(page))
    headers = []
    for label, sort_key in [('Лот и заказчик', ''), ('Сумма, ₸', 'amount'), ('Даты заявки', ''), ('Статус', 'status'), ('Дата добавления', 'added')]:
        active = key == sort_key
        headers.append({'label': label, 'sort': ('-' if active and not descending else '') + sort_key,
                        'sortable': bool(sort_key), 'arrow': ('↓' if descending else '↑') if active else '↕',
                        'aria': ('descending' if descending else 'ascending') if active else 'none'})
    return render(request, 'core/favorites.html', {'page': page, 'favorite_sort': selected, 'favorite_headers': headers})



def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return JsonResponse({"status": "ok"})
    except Exception:
        return JsonResponse({"status": "unavailable"}, status=503)


@login_required
def notification_settings(request):
    from django.conf import settings
    from .telegram_service import connection_link
    from .forms import CompanySettingsForm
    company, _ = CompanySettings.objects.get_or_create(owner=request.user)
    company_form = CompanySettingsForm(request.POST if request.method == 'POST' and request.POST.get('action') == 'company' else None,
        owner=request.user,
        initial={key:getattr(company,key,True if key == 'is_vat_payer' else '') for key in CompanySettingsForm.base_fields})
    sub = TelegramSubscription.objects.filter(owner=request.user).first()
    link = None
    if request.method == "POST":
        if request.POST.get('action') == 'company':
            if company_form.is_valid():
                from .outcome_service import reset_company
                from django.contrib import messages
                try:
                    with transaction.atomic():
                        reset_company(request.user.pk, company_form.cleaned_data['company_bin'])
                        current=CompanySettings.objects.select_for_update().get(owner=request.user)
                        for key,value in company_form.cleaned_data.items():
                            if key!='company_bin' and (key in request.POST or (key=='legal_address' and any(name in request.POST for name in ('address_city','address_street','address_house','address_office')))):setattr(current,key,value)
                        current.save()
                except IntegrityError:
                    if not CompanySettings.objects.filter(company_bin=company_form.cleaned_data['company_bin']).exclude(owner=request.user).exists():
                        raise
                    company_form.add_error('company_bin', 'Этот БИН уже используется другим аккаунтом.')
                else:
                    messages.success(request, 'Реквизиты компании сохранены.')
                    return redirect('notification_settings')
        elif request.POST.get("action") == "connect" and settings.TELEGRAM_POLLING_ENABLED and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME:
            link = connection_link(request.user)
            sub = TelegramSubscription.objects.get(owner=request.user)
        elif request.POST.get("action") == "disconnect":
            from .workflow_notifications import cancel_pending
            cancel_pending(request.user.pk)
            TelegramSubscription.objects.filter(owner=request.user).update(enabled=False,chat_id=None,link_hash="",link_expires_at=None)
            SearchProfile.objects.filter(owner=request.user).update(enabled=False)
            StatusDelivery.objects.filter(watch__owner=request.user,state="pending").update(state="cancelled")
            AnnouncementWatch.objects.filter(owner=request.user).update(enabled=False,lease="",lease_until=None)
            TelegramDelivery.objects.filter(owner=request.user,state="pending").update(state="cancelled")
            return redirect("notification_settings")
    context = {"company_form":company_form,"subscription":sub,"telegram_polling_enabled":settings.TELEGRAM_POLLING_ENABLED,"telegram_ready":bool(settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_BOT_USERNAME),"telegram_bot_username":settings.TELEGRAM_BOT_USERNAME,"connect_link":link}
    template = "core/_telegram_status.html" if request.headers.get("HX-Request") else "core/settings.html"
    response=render(request,template,context)
    response['Cache-Control']='no-store, private'
    return response


@login_required
@require_POST
def profile_notifications(request, profile_id):
    from django.contrib import messages
    profile = get_object_or_404(SearchProfile,pk=profile_id,owner=request.user)
    error = ""
    from decimal import Decimal, InvalidOperation, ROUND_CEILING
    try:
        hours = Decimal(request.POST.get("hours", "1").replace(",", "."))
        if not hours.is_finite() or not Decimal("1") <= hours <= 12 or hours != hours.to_integral_value(): raise ValueError()
        minutes = max(1, int((hours*60).to_integral_value(rounding=ROUND_CEILING)))
    except (InvalidOperation, ValueError):
        minutes = max(1,profile.interval_minutes)
        hours = Decimal(minutes)/60
        error = "Недопустимое значение интервала."
    if not error:
        if request.POST.get("action") == "toggle":
            connected = TelegramSubscription.objects.filter(owner=request.user,enabled=True,chat_id__isnull=False).exists()
            if not profile.enabled and not connected:
                error = "Сначала подключите Telegram в настройках."
            else:
                profile.enabled = not profile.enabled
                profile.next_monitor_at = None
        if profile.interval_minutes != minutes:
            profile.next_monitor_at = timezone.now()+timedelta(minutes=minutes)
            profile.collection_next_at = timezone.now()+timedelta(minutes=minutes)
        profile.interval_minutes = minutes
        profile.save(update_fields=["enabled","interval_minutes","next_monitor_at","collection_next_at","updated_at"])
    if request.headers.get("HX-Request"):
        return render(request,"core/_profile_notifications.html",{"profile":profile,"notification_error":error})
    if error: messages.error(request,error)
    return redirect("search")


@login_required
@require_POST
def status_watch(request,lot_id):
    from .workflow_state import sync_lot
    lot=get_object_or_404(Lot.objects.select_related('announcement'),pk=lot_id)
    sync_lot(request.user.pk,lot.pk)
    error='Уведомления определяются этапом лота. Измените этап в рабочем пространстве.'
    result_context(request,[lot])
    if request.headers.get("HX-Request"):
        return render(request,"core/_lot_actions.html",{"lot":lot,"update_favorite_count":True,"watch_error":error,"return_to":request.POST.get("next","")})
    if error:return redirect("notification_settings")
    next_url=request.POST.get("next","")
    if url_has_allowed_host_and_scheme(next_url,allowed_hosts={request.get_host()},require_https=request.is_secure()):return redirect(next_url)
    return redirect("lot_detail",lot_id=lot.pk)


@login_required
@require_POST
def document_retry(request, lot_id):
    lot = get_object_or_404(Lot, pk=lot_id, favorite__owner=request.user)
    from .document_service import queue_documents
    queue_documents(lot.pk)
    if request.headers.get('X-Lot-Async') == '1':
        return JsonResponse({'queued': True})
    return redirect('lot_detail',lot_id=lot.pk)


@login_required
def document_download(request, lot_id, version_id):
    from .document_service import stored_path
    from .models import UserLotWorkflow
    lot = get_object_or_404(Lot, pk=lot_id)
    if not (Favorite.objects.filter(owner=request.user,lot=lot).exists() or
            UserLotWorkflow.objects.filter(owner=request.user,lot=lot,stage='archived').exists()):
        raise Http404
    version = get_object_or_404(DocumentVersion.objects.select_related('document'),pk=version_id,document__announcement_id=lot.announcement_id)
    if version.document.lot_id not in {None,lot.pk}:
        raise Http404
    try:
        stream = stored_path(version.storage_path).open('rb')
    except (OSError, ValueError):
        raise Http404
    response = FileResponse(stream,as_attachment=True,filename=version.document.name,content_type='application/octet-stream')
    response['Cache-Control'] = 'private, no-store'
    return response
