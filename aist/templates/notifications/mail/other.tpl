{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}
<html>
    <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    </head>
    <body style="font-family: Arial, sans-serif; color: #333333; margin: 0; padding: 0;">
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
            <hr style="border: none; border-top: 1px solid #e0e0e0; margin: 16px 0;"/>

            <!-- Signature: logo left, text right -->
            <table cellpadding="0" cellspacing="0" border="0" style="border-collapse: collapse;">
                <tr>
                    <td style="padding: 0 16px 0 0; vertical-align: middle; border-right: 2px solid #e0e0e0;">
                        <img
                            src="{{ '/static/aist/logo.jpg'|full_url }}"
                            alt="AIST"
                            style="height: 72px; width: auto; display: block;"
                        />
                    </td>
                    <td style="padding: 0 0 0 16px; vertical-align: middle;">
                        <p style="margin: 0; font-size: 13px; color: #555555;">{% trans "Best regards" %},</p>
                        <p style="margin: 4px 0 0; font-size: 15px; font-weight: bold; color: #1a1a1a;">AIST Security Team</p>
                        <p style="margin: 2px 0 0; font-size: 12px; color: #777777;">Application Security &amp; Risk Management</p>
                    </td>
                </tr>
            </table>

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
