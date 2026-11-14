"""Search folders shown in the UI; manual storage belongs to the aggregate."""
from .models import SearchProfile


def saved_profiles(owner):
    owned = SearchProfile.objects.filter(owner=owner)
    # A subquery also handles JSON objects without the manual key on both DBs.
    return owned.exclude(pk__in=owned.filter(filters__manual=True).values('pk'))
