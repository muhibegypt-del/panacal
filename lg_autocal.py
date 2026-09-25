"""LG OLED AutoCal on a Windows PC. See README.md."""
import sys

if sys.version_info < (3, 8):
    sys.exit(f"LG AutoCal needs Python 3.8 or newer; this is Python {sys.version.split()[0]}. "
             "Install a current Python from python.org and run again.")

from lgcal.app import main  # noqa: E402  (after the version check)

if __name__ == "__main__":
    sys.exit(main())
