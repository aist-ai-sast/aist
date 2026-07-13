{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
{% url 'view_risk_acceptance' risk_acceptance.engagement.id risk_acceptance.id as risk_acceptance_url %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {{ description }}
</p>
<p style="font-size:14px; color:#334155;">
  {% if risk_acceptance.is_expired %}
    {% blocktranslate with risk_url=risk_acceptance_url|full_url risk_findings=risk_acceptance.accepted_findings.all|length risk_date=risk_acceptance.expiration_date_handled|date %}<a href="{{risk_url}}" style="color:#2bb7e6;">Risk acceptance {{ risk_acceptance }}</a> with {{ risk_findings }} has expired {{ risk_date }}{% endblocktranslate %}
  {% else %}
    {% blocktranslate with risk_url=risk_acceptance_url|full_url risk_findings=risk_acceptance.accepted_findings.all|length risk_date=risk_acceptance.expiration_date|date %}<a href="{{risk_url}}" style="color:#2bb7e6;">Risk acceptance {{ risk_acceptance }}</a> with {{ risk_findings }} will expire {{ risk_date }}{% endblocktranslate %}
  {% endif %}
</p>
{% if risk_acceptance.reactivate_expired %}
  <p style="font-size:13px; color:#55617a;">{% blocktranslate %}Findings have been reactivated{% endblocktranslate %}</p>
{% endif %}
{% if risk_acceptance.restart_sla_expired %}
  <p style="font-size:13px; color:#55617a;">{% blocktranslate %}Findings SLA start date have been reset{% endblocktranslate %}</p>
{% endif %}
<p style="font-size:13px; color:#334155;">
  {% trans "Findings" %}:<br/>
  {% for finding in risk_acceptance.accepted_findings.all %}
    {% url 'view_finding' finding.id as finding_url %}
    <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">{{ finding.title }}</a> ({{ finding.severity }}) {{ finding.status }}<br/>
  {% empty %}
    {% trans "None" %}<br/>
  {% endfor %}
</p>
{% endblock %}
