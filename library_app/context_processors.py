from .models import LibrarySettings


def library_rules(request):
    """Makes the Admin-configured rules available in every template as {{ rules.* }}."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    return {'rules': LibrarySettings.get()}
