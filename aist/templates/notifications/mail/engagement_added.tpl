{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
{% url 'view_product' engagement.product.id as product_url %}
{% url 'view_engagement' engagement.id as engagement_url %}
<p style="font-size:14px; line-height:22px; color:#334155;">
  {% blocktranslate trimmed with engagement_name=engagement.name engagement_product=engagement.product prod_url=product_url|full_url eng_url=engagement_url|full_url %}
    The engagement "{{ engagement_name }}" has been created in the product "{{ engagement_product }}". It can be viewed here: <a href="{{prod_url}}">{{ engagement_product }}</a> / <a href="{{eng_url}}">{{ engagement_name }}</a>
  {% endblocktranslate %}
</p>
{% endblock %}
