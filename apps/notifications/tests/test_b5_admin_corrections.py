# mypy: disable-error-code="no-untyped-def"
import json

import pytest
from django.contrib.auth.models import User

from apps.notifications.models import WelcomeTemplate
from apps.notifications.tests.test_b4_delivery_welcome import KEYBOARD

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize(
    "body",
    [
        "Welcome 👋",
        "x" * 4094 + "\nX",
        '<b>Welcome 👋</b>\n<pre>A &amp; B</pre>\n<a href="https://t.me/Dalexio_Official">Join</a>',
    ],
)
def test_actual_welcome_admin_post_normalizes_multiline(client, newline, body):
    actor = User.objects.create_superuser("multiline", password="synthetic")
    client.force_login(actor)
    response = client.post(
        "/admin/notifications/welcometemplate/add/",
        {
            "version": "1",
            "body": body.replace("\n", newline),
            "keyboard": json.dumps(KEYBOARD),
            "_save": "Save",
        },
    )
    assert response.status_code == 302, response.context and response.context["adminform"].errors
    assert WelcomeTemplate.objects.get().body == body


@pytest.mark.parametrize(
    "body",
    [
        "<b>Good</b>\r\n<script>bad</script>",
        "<b>Unbalanced\r\ntext",
        '<a href="javascript:x">Bad</a>\ntext',
        "Hello\r\n{user}",
        "Hello\n\x01",
    ],
)
def test_actual_welcome_admin_post_keeps_injection_validation(client, body):
    actor = User.objects.create_superuser("invalid-multiline")
    client.force_login(actor)
    response = client.post(
        "/admin/notifications/welcometemplate/add/",
        {
            "version": "1",
            "body": body,
            "keyboard": json.dumps(KEYBOARD),
            "_save": "Save",
        },
    )
    assert response.status_code == 200
    assert response.context["adminform"].errors
    assert not WelcomeTemplate.objects.exists()
