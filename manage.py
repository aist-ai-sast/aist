#!/usr/bin/env python
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    vendor_dir = base_dir / "vendor" / "defectdojo"
    if str(vendor_dir) not in sys.path:
        sys.path.insert(0, str(vendor_dir))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "aist_site.settings")

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
