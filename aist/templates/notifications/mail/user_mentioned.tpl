{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed %}
    User {{ requested_by }} jotted a note on {{ section }}:<br/>
    <br/>
    {{ note }}<br/>
    <br/>
    It can be reviewed at <a href="{{ url }}">{{ url }}</a>
  {% endblocktranslate %}
</p>
{% endblock %}
