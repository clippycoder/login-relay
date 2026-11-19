# SPDX-License-Identifier: GPL-3.0-or-later
import sys


def run():
    if "--version" in sys.argv[1:]:
        from . import VERSION
        print("loginrelay " + VERSION)
        return 0
    try:
        from .app import main
    except (ImportError, ValueError) as e:
        sys.stderr.write(
            "Login Relay needs PyGObject and GTK 3 (Debian/Ubuntu: sudo apt install python3-gi "
            "gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1).\n%s\n" % e)
        return 1
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(run())
