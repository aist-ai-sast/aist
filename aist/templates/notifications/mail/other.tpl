{% extends "notifications/mail/_base.tpl" %}
{% load i18n %}
{% load navigation_tags %}
{% load display_tags %}

{% block body %}
<div style="font-size:14px; line-height:22px; color:#334155;">
  {{ description|markdown_render }}
</div>
{% if url is not None %}
  <table role="presentation" cellpadding="0" cellspacing="0" class="aist-email-cta-table" style="margin-top:20px;">
    <tr>
      <td style="border-radius:8px; background-color:#2bb7e6;">
        <a href="{{ url|full_url }}"
           class="aist-email-cta"
           style="display:inline-block; padding:10px 22px; font-size:13px; font-weight:600; color:#ffffff; text-decoration:none; border-radius:8px;">
          {% trans "View details" %}
        </a>
      </td>
    </tr>
  </table>
{% endif %}

<div style="margin-top:24px; padding-top:16px; border-top:1px solid #e7ecf4;">
  <p style="margin:0; font-size:12px; color:#55617a;">{% trans "Best regards" %},</p>
  <p style="margin:4px 0 0; font-size:13px; font-weight:600; color:#16213a;">AIST Security Team</p>
  <p style="margin:2px 0 0; font-size:11px; color:#7c879e;">Application Security &amp; Risk Management</p>
</div>
{% endblock %}

{% block footer %}
{% if system_settings.disclaimer_notifications and system_settings.disclaimer_notifications.strip %}
<div style="background-color:#eef1f7; border:1px solid #dfe6f2; border-radius:8px; padding:12px;">
  <div style="font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:#55617a; margin-bottom:4px;">{% trans "Disclaimer" %}</div>
  <p style="margin:0; font-size:12px; color:#55617a;">{{ system_settings.disclaimer_notifications }}</p>
</div>
{% endif %}
{% endblock %}
