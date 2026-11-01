{% load i18n %}
{% load display_tags %}
{# The description arrives escaped for Slack's three control characters (aist.actions.escape_slack_text);
   Django's HTML escaping would additionally emit entities Slack never decodes. #}
{{ description|safe }}
{% if url is not None %}
{% blocktranslate trimmed with event_url=url|full_url %}
    More information on this event can be found here: {{ event_url }}
{% endblocktranslate %}
{% endif %}
{% if system_settings.disclaimer_notifications|length %}

    {% trans "Disclaimer" %}:
    {{ system_settings.disclaimer_notifications }}
{% endif %}
