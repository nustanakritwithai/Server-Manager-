"""Write a scrypt hash of the admin password on stdin.

deploy/windows/set-admin-password.ps1 runs this with the project venv.
The password is not echoed and is not included in the hash string.
"""

from __future__ import annotations

import sys

from simcore.admin_auth import hash_admin_password


def main() -> None:
    sys.stdout.write(hash_admin_password(sys.stdin.read()))


if __name__ == "__main__":
    main()
