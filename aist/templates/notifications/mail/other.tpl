{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}
<html>
    <body>
        {% autoescape on %}
            <p>
                {% trans "Hello" %},
            </p>
            <div>
                {{ description|markdown_render }}
            </div>
            {% if url is not None %}
                <br/>
                <br/>
              {% blocktranslate trimmed with event_url=url|full_url %}
                More information on this event can be found here: <a href="{{ event_url }}">{{ event_url }}</a>
              {% endblocktranslate %}
            {% endif %}
            <br/>
            <br/>
            <p style="margin: 0;">
                Best regards,
            </p>
            <p style="margin: 8px 0 0;">
                AIST Security Team
            </p>
            <p style="margin: 4px 0 0;">
                Application Security &amp; Risk Management
            </p>
            <p style="margin: 10px 0 0;">
                <img
                    src="{{ '/static/aist/logo.jpg'|full_url }}"
                    alt="AIST"
                    style="height: 28px; width: auto; display: block;"
                />
            </p>
            {% if system_settings.disclaimer_notifications and system_settings.disclaimer_notifications.strip %}
                <br/>
                <div style="background-color:#DADCE2; border:1px #003333; padding:.8em; ">
                    <span style="font-size:16pt;  font-family: 'Cambria','times new roman','garamond',serif; color:#ff0000;">{% trans "Disclaimer" %}</span><br/>
                    <p style="font-size:11pt; line-height:10pt; font-family: 'Cambria','times roman',serif;">{{ system_settings.disclaimer_notifications }}</p>
                </div>
            {% endif %}
        {% endautoescape %}
    </body>
</html>
