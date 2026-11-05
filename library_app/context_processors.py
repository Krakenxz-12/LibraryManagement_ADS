from .models import LibrarySettings


def library_rules(request):
    """Makes the Admin-configured rules available in every template as {{ rules.* }}."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {}
    return {'rules': LibrarySettings.get()}



def unread_notifications(request):
    """Expose the signed-in user's unread notification count to every page."""
    user = getattr(request, 'user', None)
    if not user or not user.is_authenticated:
        return {'unread_notifications_count': 0}

    return {
        'unread_notifications_count': user.notifications.filter(is_read=False).count(),
    }
