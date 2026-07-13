{% extends "aist/email/_base.html" %}
{% load i18n %}
{% load display_tags %}

{% comment %}
Shared chrome for every DefectDojo/AIST notification-mail template (other.tpl
and its siblings under this directory). Consumers only fill {% block body %}
with their event-specific content — the greeting, notification-settings link,
and disclaimer box are handled once, here, instead of being duplicated (along
with a "Defect Dojo" fallback signature) in every individual template.
{% endcomment %}

{% block content %}
{% autoescape on %}
<p style="margin:0 0 16px; font-size:14px; color:#334155;">
  {% trans "Hello" %}{% if user %} {{ user.get_full_name }}{% endif %},
</p>
{% block body %}{% endblock %}
{% endautoescape %}
{% endblock %}

{% block footer %}
{% url 'notifications' as notification_url %}
<p style="margin:0 0 8px;">
  {% trans "You can manage your notification settings here" %}:
  <a href="{{ notification_url|full_url }}" style="color:#2bb7e6;">{{ notification_url|full_url }}</a>
</p>
{% if system_settings.disclaimer_notifications and system_settings.disclaimer_notifications.strip %}
<div style="margin-top:8px; background-color:#eef1f7; border:1px solid #dfe6f2; border-radius:8px; padding:12px;">
  <div style="font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:#55617a; margin-bottom:4px;">{% trans "Disclaimer" %}</div>
  <p style="margin:0; font-size:12px; color:#55617a;">{{ system_settings.disclaimer_notifications }}</p>
</div>
{% endif %}
{% endblock %}
