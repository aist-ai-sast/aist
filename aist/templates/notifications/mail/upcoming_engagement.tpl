{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed with product=engagement.product %}
    this is a reminder that the engagement "{{ product }}" is about to start shortly.
  {% endblocktranslate %}
</p>
<p style="font-size:13px; color:#55617a;">
  {% trans "Project start" %}: {{ engagement.target_start }}<br/>
  {% trans "Project end" %}: {{ engagement.target_end }}
</p>
{% endblock %}
