{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed with prod_url=url|full_url %}
    The new product type "{{ title }}" has been added. It can be viewed here: <a href="{{ prod_url }}">{{ title }}</a>
  {% endblocktranslate %}
</p>
{% endblock %}
