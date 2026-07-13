{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
{% url 'view_finding' finding.id as finding_url %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% if sla_age < 0 %}
    {% blocktranslate trimmed %}
      This security finding has breached its SLA.

      - Day(s) overdue: {{sla}}
    {% endblocktranslate %}
  {% else %}
    {% blocktranslate trimmed %}
      A security finding is about to breach its SLA.

      - Day(s) remaining: {{sla}}
    {% endblocktranslate %}
  {% endif %}
</p>
<p style="font-size:13px; color:#55617a;">
  - {% trans "Title" %}: <a href="{{finding_url|full_url}}" style="color:#2bb7e6;">{{finding.title}}</a><br/>
  - {% trans "Severity" %}: {{finding.severity}}<br/><br/>
  {% trans "Please refer to your SLA documentation for further guidance" %}
</p>
{% endblock %}
