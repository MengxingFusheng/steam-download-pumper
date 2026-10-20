from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return 'scrypt$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(digest).decode()


def verify_password(password, encoded):
    try:
        kind, salt, digest = encoded.split('$')
        if kind != 'scrypt':
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt, validate=True), n=16384, r=8, p=1)
        return hmac.compare_digest(actual, base64.b64decode(digest, validate=True))
    except (ValueError, TypeError):
        return False


if __name__ == '__main__':
    password = getpass.getpass('Dashboard admin password: ')
    if len(password) < 12:
        raise SystemExit('Use at least 12 characters')
    print(hash_password(password))
