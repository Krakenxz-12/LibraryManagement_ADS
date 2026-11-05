from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import Member, UserProfile


class LibrarySocialAccountAdapter(DefaultSocialAccountAdapter):
    """Allow only PSU Google accounts and create them as Members, never staff."""

    def _has_allowed_email_domain(self, email):
        domain = getattr(settings, 'PSU_EMAIL_DOMAIN', 'psu.palawan.edu.ph').strip().lower()
        email = (email or '').strip().lower()
        return bool(domain and email.endswith(f'@{domain}'))

    def pre_social_login(self, request, sociallogin):
        """Reject non-PSU accounts before either login or signup can complete."""
        email = getattr(sociallogin.user, 'email', '')
        if not self._has_allowed_email_domain(email):
            messages.error(
                request,
                'Access denied. Please sign in using your @psu.palawan.edu.ph university account.'
            )
            raise ImmediateHttpResponse(redirect('login'))

    def is_open_for_signup(self, request, sociallogin):
        """Apply the same domain rule to new social-account registrations."""
        email = getattr(sociallogin.user, 'email', '')
        return self._has_allowed_email_domain(email)

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
