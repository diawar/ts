from django.http import Http404
from django.shortcuts import render
from django.views.decorators.http import require_safe
from .guides import GUIDES, GUIDE_BY_SLUG


@require_safe
def guides(request):
    return render(request, 'public/guides.html', {'guides': GUIDES})


@require_safe
def guide(request, slug):
    article = GUIDE_BY_SLUG.get(slug)
    if not article:
        raise Http404
    related = [item for item in GUIDES if item['slug'] != slug]
    return render(request, 'public/guide.html', {'guide': article, 'related_guides': related[:3]})
