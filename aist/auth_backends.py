"""
Session-login authentication backend that resolves the submitted identifier
as an email address.

Invited accounts get ``username`` set to the local-part of their email and
``email`` set to the full address (see ``aist/members/service.py``'s
``_unique_username``) — two different stored identifiers. Every UI surface
(the Users list, invite emails, notifications) shows the email, so users
naturally type it back at login. ``django.contrib.auth.backends.ModelBackend``
only resolves ``USERNAME_FIELD`` (``username``), so login-by-email always
failed with "invalid credentials" even with the correct password.

This backend only ADDS email resolution — it returns ``None`` (declines) for
any identifier that doesn't match a user's email, letting Django's
``authenticate()`` loop fall through to the existing ``ModelBackend`` entry
for username-based lookup. It never duplicates or replaces that lookup.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model

User = get_user_model()


class EmailBackend:
    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        try:
            user = User.objects.get(email__iexact=username)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            # Hash a dummy password so a nonexistent/ambiguous email takes
            # roughly as long as a real one — mirrors ModelBackend's own
            # countermeasure against timing-based user enumeration.
            User().set_password(password)
            return None
        if not user.is_active or not user.check_password(password):
            return None
        return user

    def get_user(self, user_id):
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
