{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<p style="font-size:14px; color:#334155;">
  {% trans "Product summary" %}:
</p>
<ul style="margin:0 0 12px; padding-left:20px; font-size:13px; color:#334155;">
  <li>{% trans "name" %}: {{ product.name }}</li>
  <li>{% trans "product type" %}: {{ product.prod_type }}</li>
  <li>{% trans "team manager" %}: {{ product.team_manager }}</li>
  <li>{% trans "product manager" %}: {{ product.product_manager }}</li>
  <li>{% trans "technical contact" %}: {{ product.technical_contact }}</li>
</ul>
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% if breach_kind == 'breached' %}
    {% blocktranslate trimmed %}These security findings have breached their SLA:{% endblocktranslate %}
  {% elif breach_kind == 'prebreach' %}
    {% blocktranslate trimmed %}These security findings are about to breach their SLA:{% endblocktranslate %}
  {% elif breach_kind == 'breaching' %}
    {% blocktranslate trimmed %}These security findings breaching their SLA today:{% endblocktranslate %}
  {% else %}
    This should not happen, check 'breach_kind' and 'kind' properties value in the source code.
  {% endif %}
</p>
<ul style="margin:0 0 12px; padding-left:20px; font-size:13px; color:#334155;">
  {% for f in findings %}
    {% url 'view_finding' f.id as finding_url %}
    <li>
      <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">"{{ f.title }}"</a> ({{ f.severity }} {% trans "severity" %}), {% trans "SLA age" %}: {{ f.sla_age }}
    </li>
  {% endfor %}
</ul>
<p style="font-size:13px; color:#55617a;">{% trans "Please refer to your SLA documentation for further guidance" %}</p>
{% endblock %}
