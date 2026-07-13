{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed %}
    User {{ requested_by }} has requested that the following users review the finding "{{ finding }}" for accuracy:
  {% endblocktranslate %}
</p>
<ul style="margin:0 0 12px; padding-left:20px; font-size:14px; color:#334155;">
  {% for user in reviewers %}
    <li>{{ user.get_full_name }}</li>
  {% endfor %}
</ul>
<p style="font-size:14px; color:#334155;">{{ note }}</p>
<p style="font-size:13px; color:#55617a;">
  {% trans "It can be reviewed at" %} <a href="{{ url|full_url }}" style="color:#2bb7e6;">{{ url|full_url }}</a>
</p>
{% endblock %}
