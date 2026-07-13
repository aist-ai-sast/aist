{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
{% url 'view_product' test.engagement.product.id as product_url %}
{% url 'view_engagement' test.engagement.id as engagement_url %}
{% url 'view_test' test.id as test_url %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed with prod_url=product_url|full_url eng_url=engagement_url|full_url eng_name=engagement.name t_url=test_url|full_url %}
    A new test has been added: <a href="{{prod_url}}">{{product}}</a> / <a href="{{eng_url}}">{{ eng_name }}</a> / <a href="{{ t_url }}">{{ test }}</a><br/>
    Finding details in the 'scan_added' email, which is a separate notification (for now).
  {% endblocktranslate %}
</p>
{% endblock %}
