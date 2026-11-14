"""Search metadata for public pages; accounts and preview hosts stay out of search."""
import json
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_safe

from .guides import GUIDES, GUIDE_BY_SLUG
from .catalog_sections import active_section_paths

CATALOG_PAGES = {'catalog', 'catalog_category', 'catalog_region', 'catalog_lot'}
PUBLIC_PAGES = {'landing', 'guides', 'guide'} | CATALOG_PAGES
TITLE = 'Поиск тендеров в Казахстане и CRM — TenderSearch'
DESCRIPTION = ('Тендеры и госзакупки Казахстана: поиск по лотам и техспецификациям, '
               'личные подборки по ключевым словам, Telegram и встроенная CRM.')
GUIDES_TITLE = 'Статьи о тендерах и госзакупках Казахстана — TenderSearch'
GUIDES_DESCRIPTION = ('Практические статьи для поставщиков: перечень изъятий, ЗЦП, поиск тендеров, '
                      'ключевые слова, техспецификации, расчёт цены и CRM для закупок.')


def site_url(request):
    configured = settings.PUBLIC_SITE_URL.strip()
    if not configured:
        return request.build_absolute_uri('/')
    parsed = urlsplit(configured)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ImproperlyConfigured('PUBLIC_SITE_URL must be an absolute site origin, e.g. https://tender-search.kz')
    return configured.rstrip('/') + '/'


def metadata(request):
    match = request.resolver_match
    name = match.url_name if match else None
    article = GUIDE_BY_SLUG.get(match.kwargs.get('slug')) if name == 'guide' else None
    title, description = {
        'login': ('Вход — TenderSearch', 'Войдите в TenderSearch: ваши подборки тендеров и CRM для закупок.'),
        'register': ('Регистрация — TenderSearch', 'Создайте аккаунт TenderSearch: поиск тендеров и встроенная CRM.'),
        'account_access': ('Срок доступа — TenderSearch', 'Управление доступом к TenderSearch.'),
        'guides': (GUIDES_TITLE, GUIDES_DESCRIPTION),
    }.get(name, (TITLE, DESCRIPTION))
    if article:
        title, description = article['seo_title'], article['description']
    catalog = getattr(request, 'public_catalog_meta', None)
    if catalog:
        title, description = catalog['title'], catalog['description']
    is_public = name in PUBLIC_PAGES and (name != 'guide' or article is not None)
    if name in CATALOG_PAGES and not catalog:
        is_public = False
    root = site_url(request) if is_public else ''
    path = article['path'].lstrip('/') if article else ('guides/' if name == 'guides' else '')
    if catalog:
        path = catalog['path'].lstrip('/')
    canonical = root + path if is_public else ''
    graph = []
    if is_public:
        organization = {'@id': root+'#organization'}
        graph.append({'@type': 'Organization', **organization, 'name': 'TenderSearch', 'url': root})
        graph.append({'@type': 'WebSite', '@id': root+'#website', 'url': root,
                      'name': 'TenderSearch', 'description': DESCRIPTION, 'inLanguage': 'ru-KZ',
                      'publisher': organization})
        if name == 'guides':
            graph.append({'@type': 'CollectionPage', '@id': canonical, 'url': canonical,
                          'name': title, 'description': description,
                          'isPartOf': {'@id': root+'#website'},
                          'mainEntity': {'@type': 'ItemList', 'itemListElement': [
                              {'@type': 'ListItem', 'position': index, 'url': root+item['path'].lstrip('/'),
                               'name': item['title']} for index, item in enumerate(GUIDES, 1)]}})
        if article:
            graph.append({'@type': 'Article', '@id': canonical+'#article',
                          'url': canonical, 'mainEntityOfPage': canonical, 'headline': article['title'],
                          'description': description, 'inLanguage': 'ru-KZ',
                          'articleSection': article['category'], 'author': organization, 'publisher': organization,
                          'datePublished': article['published_at'].isoformat(),
                          'dateModified': article['updated_at'].isoformat()})
        if catalog:
            graph.append({'@type': 'WebPage' if name == 'catalog_lot' else 'CollectionPage',
                          '@id': canonical, 'url': canonical, 'name': title,
                          'description': description, 'isPartOf': {'@id': root+'#website'}})
        if name != 'landing':
            breadcrumbs = [{'@type': 'ListItem', 'position': 1, 'name': 'Главная', 'item': root},
                           {'@type': 'ListItem', 'position': 2, 'name': 'Статьи', 'item': root+'guides/'}]
            if article:
                breadcrumbs.append({'@type': 'ListItem', 'position': 3, 'name': article['title'], 'item': canonical})
            if catalog:
                breadcrumbs[1] = {'@type': 'ListItem', 'position': 2, 'name': 'Каталог закупок', 'item': root+'catalog/'}
                if name != 'catalog':
                    breadcrumbs.append({'@type': 'ListItem', 'position': 3, 'name': catalog['name'], 'item': canonical})
            graph.append({'@type': 'BreadcrumbList', 'itemListElement': breadcrumbs})
    # Escape HTML delimiters before inserting JSON into a script element.
    structured = json.dumps({'@context': 'https://schema.org', '@graph': graph}, ensure_ascii=False).translate(
        {ord('<'): r'\u003C', ord('>'): r'\u003E', ord('&'): r'\u0026'}) if graph else ''
    return {'seo_title': title, 'seo_description': description,
            'seo_indexable': is_public and request.build_absolute_uri('/') == root and (not catalog or catalog.get('indexable', True)),
            'seo_canonical': canonical, 'seo_og_type': 'article' if article else 'website',
            'seo_schema': format_html('<script type="application/ld+json">{}</script>',
                                      mark_safe(structured)) if structured else ''}


@require_safe
def robots(request):
    return HttpResponse('User-agent: *\nAllow: /\n\nSitemap: '+site_url(request)+'sitemap.xml\n',
                        content_type='text/plain; charset=utf-8')


@require_safe
def sitemap(request):
    root = site_url(request)
    locations = [root, root+'catalog/', root+'guides/', *(root+article['path'].lstrip('/') for article in GUIDES)]
    locations += [root+path.lstrip('/') for path in active_section_paths()]
    entries = format_html_join('', '<url><loc>{}</loc></url>', ((url,) for url in locations))
    xml = format_html('<?xml version="1.0" encoding="UTF-8"?>\n'
                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{}</urlset>\n', entries)
    return HttpResponse(xml, content_type='application/xml; charset=utf-8')


class SearchIndexingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = request.resolver_match
        alternate_origin = settings.PUBLIC_SITE_URL and request.build_absolute_uri('/') != site_url(request)
        catalog = getattr(request, 'public_catalog_meta', {})
        if alternate_origin or not catalog.get('indexable', True) or not match or match.url_name not in PUBLIC_PAGES | {'robots', 'sitemap'} or response.status_code >= 400:
            response['X-Robots-Tag'] = 'noindex, nofollow'
        return response
