"""WSGI config for the AIST product."""

import os
import sys
from pathlib import Path

PRODUCT_BASE_DIR = Path(__file__).resolve().parent.parent
VENDOR_BASE_DIR = PRODUCT_BASE_DIR / "vendor" / "defectdojo"

if str(VENDOR_BASE_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aist_site.settings")

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
