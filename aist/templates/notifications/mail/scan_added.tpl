{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
{% url 'view_product' test.engagement.product.id as product_url %}
{% url 'view_engagement' test.engagement.id as engagement_url %}
{% url 'view_test' test.id as test_url %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {{ description }}
  <br/><br/>
  {% blocktranslate %}{{ finding_count }} findings have been updated for while a scan was uploaded{% endblocktranslate %}:
  <a href="{{product_url|full_url}}" style="color:#2bb7e6;">{{product}}</a> / <a href="{{engagement_url|full_url}}" style="color:#2bb7e6;">{{ engagement.name }}</a> / <a href="{{ test_url|full_url }}" style="color:#2bb7e6;">{{ test }}</a>
</p>
<details style="margin-bottom:12px;">
  <summary style="font-size:13px; color:#334155;">{% blocktranslate %}New findings{% endblocktranslate %} ({{ findings_new|length }})</summary>
  <div style="font-size:13px; color:#334155; margin-top:6px;">
    {% for finding in findings_new %}
      {% url 'view_finding' finding.id as finding_url %}
      <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">{{ finding.title }}</a> ({{ finding.severity }})<br/>
    {% empty %}
      {% trans "None" %}<br/>
    {% endfor %}
  </div>
</details>
<details style="margin-bottom:12px;">
  <summary style="font-size:13px; color:#334155;">{% blocktranslate %}Reactivated findings{% endblocktranslate %} ({{ findings_reactivated|length }})</summary>
  <div style="font-size:13px; color:#334155; margin-top:6px;">
    {% for finding in findings_reactivated %}
      {% url 'view_finding' finding.id as finding_url %}
      <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">{{ finding.title }}</a> ({{ finding.severity }})<br/>
    {% empty %}
      {% trans "None" %}<br/>
    {% endfor %}
  </div>
</details>
<details style="margin-bottom:12px;">
  <summary style="font-size:13px; color:#334155;">{% blocktranslate %}Closed findings{% endblocktranslate %} ({{ findings_mitigated|length }})</summary>
  <div style="font-size:13px; color:#334155; margin-top:6px;">
    {% for finding in findings_mitigated %}
      {% url 'view_finding' finding.id as finding_url %}
      <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">{{ finding.title }}</a> ({{ finding.severity }})<br/>
    {% empty %}
      {% trans "None" %}<br/>
    {% endfor %}
  </div>
</details>
<details>
  <summary style="font-size:13px; color:#334155;">{% blocktranslate %}Untouched findings{% endblocktranslate %} ({{ findings_untouched|length }})</summary>
  <div style="font-size:13px; color:#334155; margin-top:6px;">
    {% for finding in findings_untouched %}
      {% url 'view_finding' finding.id as finding_url %}
      <a href="{{ finding_url|full_url }}" style="color:#2bb7e6;">{{ finding.title }}</a> ({{ finding.severity }})<br/>
    {% empty %}
      {% trans "None" %}<br/>
    {% endfor %}
  </div>
</details>
{% endblock %}
