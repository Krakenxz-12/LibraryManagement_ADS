from django.db import transaction
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import Member, UserProfile


class LibrarySocialAccountAdapter(DefaultSocialAccountAdapter):
    """Create Google-only accounts as Members, never as staff accounts."""

    @transaction.atomic
    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        # New social signups always receive the MEMBER role. Existing Admin and
        # Librarian accounts are linked by their verified Google email instead.
        UserProfile.objects.get_or_create(
            user=user,
            defaults={'role': 'MEMBER'},
        )
        profile = UserProfile.objects.get(user=user)
        if profile.role not in ('ADMIN', 'LIBRARIAN'):
            profile.role = 'MEMBER'
            profile.save(update_fields=['role'])

        email = (user.email or '').strip().lower()
        if email:
            member = Member.objects.filter(email__iexact=email).first()
            if member:
                if member.user_id is None:
                    member.user = user
                    member.save(update_fields=['user'])
            else:
                Member.objects.create(
                    user=user,
                    member_id=Member.generate_member_id(),
                    first_name=user.first_name or user.username,
                    last_name=user.last_name or '',
                    email=email,
                    phone='',
                    membership_type='STUDENT',
                    status='ACTIVE',
                )

        return user
