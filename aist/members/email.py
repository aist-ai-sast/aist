"""
Set-password / invite emails for organization members.

We do NOT reuse the vendor password-reset page (it lives under the admin prefix,
is blocked for anonymous users, and is not AIST-branded). Instead we mint a
standard Django reset token and email a link to the AIST client-ui set-password
page, rendered with AIST-branded HTML templates. Distinct copy per purpose
(invite vs reset), shared visual style via ``aist/email/_base.html``.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.exceptions import ValidationError

User = get_user_model()

# Copy per email purpose; the visual style is shared by the base template.
_PURPOSES = {
    "invite": {
        "subject": "You have been invited to AIST",
        "heading": "Welcome to AIST",
        "intro": "An administrator has invited you to AIST. Set your password to activate your account and sign in.",
        "cta": "Set your password",
    },
    "reset": {
        "subject": "Reset your AIST password",
        "heading": "Reset your password",
        "intro": "We received a request to set a new password for your AIST account.",
        "cta": "Choose a new password",
    },
}


def send_set_password_email(user: User, *, purpose: str = "invite") -> None:
    """
    Email ``user`` an AIST-branded link to set their own password.

    ``purpose`` selects the copy ("invite" for new members, "reset" for an
    admin-triggered reset). Raises ``ValidationError`` if the user has no email
    or delivery fails, so the caller surfaces the problem instead of hiding it.
    """
    if not user.email:
        msg = "User has no email address to send the link to."
        raise ValidationError({"email": msg})

    copy = _PURPOSES.get(purpose, _PURPOSES["invite"])
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = f"{settings.SITE_URL.rstrip('/')}/auth/set-password/{uid}/{token}/"

    context = {
        "heading": copy["heading"],
        "intro": copy["intro"],
        "cta": copy["cta"],
        "action_url": link,
        "recipient_name": user.first_name or user.username,
    }
    html_body = render_to_string("aist/email/set_password.html", context)
    text_body = render_to_string("aist/email/set_password.txt", context)

    message = EmailMultiAlternatives(subject=copy["subject"], body=text_body, to=[user.email])
    message.attach_alternative(html_body, "text/html")
    try:
        message.send()
    except Exception as exc:
        msg = "Could not send the email. Check the mail server configuration."
        raise ValidationError({"email": msg}) from exc
