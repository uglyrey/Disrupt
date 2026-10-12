"""Generate an observer's password and authenticator bundle into a private owner export.

Run inside the API container with a private /enroll volume. Output never includes credentials.
Download and verify the bundle, then remove the temporary server copy. The account stays
inactive until its recipient confirms a code through the owner's enrollment helper.
"""

import getpass
import os
import secrets
from pathlib import Path

from app import admin_cli, totp

EXPORT_DIRECTORY = Path("/enroll")


def main(argv=None):
    parser = admin_cli.build_parser()
    args = parser.parse_args(argv)
    if args.command != "create" or args.role != "observer":
        parser.error("Only create --role observer is supported")
    if not admin_cli.USERNAME.fullmatch(args.username):
        parser.error("Use a lowercase observer login")
    if EXPORT_DIRECTORY.is_symlink() or not EXPORT_DIRECTORY.is_dir():
        parser.error("Missing private export directory")
    target = EXPORT_DIRECTORY / (args.username + ".txt")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    password = secrets.token_urlsafe(24)
    original_getpass, original_show = getpass.getpass, admin_cli.show_secret
    exported = False

    def export(account, secret):
        nonlocal exported
        bundle = (
            "Beresta observer. Read-only statistics and exports.\n"
            "URL: " + admin_cli.Settings.from_env().public_origin + "/review\n"
            "Login: " + account.username + "\nPassword: " + password + "\n\n"
            "Authenticator setup URI:\n" + totp.otpauth_uri(secret, account.username) + "\n\n"
            "Manual setup key:\n" + secret + "\n\n"
            "Access expires (Unix seconds): " + str(account.access_expires_at) + "\n"
            "Import the setup key into an authenticator. Ask the owner to confirm a current code.\n"
            "Keep the password and authenticator key private. Do not share this account.\n"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle)
            stream.flush()
            os.fsync(stream.fileno())
        exported = True
        print("Observer created. Private bundle saved. Authenticator confirmation is required.")

    getpass.getpass = lambda prompt="": password
    admin_cli.show_secret = export
    try:
        return admin_cli.main(argv)
    finally:
        getpass.getpass, admin_cli.show_secret = original_getpass, original_show
        if not exported:
            try:
                os.close(descriptor)
            except OSError:
                pass
            if target.is_file() and target.stat().st_size == 0:
                target.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
