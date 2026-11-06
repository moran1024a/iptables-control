import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import database
import settings


def reset_admin(conn):
    database.set_setting(conn, "admin_user", settings.DEFAULT_USER)
    database.set_setting(conn, "admin_hash", hash_password(settings.DEFAULT_PASS))
    database.set_setting(conn, "must_change", "1")
    conn.execute("DELETE FROM sessions")


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(password, stored):
    try:
        _, salt, h = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def new_key():
    return secrets.token_hex(32)


def derive(key):
    k = bytes.fromhex(key)
    return (
        hmac.new(k, b"ictl-enc", hashlib.sha256).hexdigest(),
        hmac.new(k, b"ictl-mac", hashlib.sha256).digest(),
    )


def aes(ek, data, decrypt=False):
    args = [
        "openssl",
        "enc",
        "-aes-256-ctr",
        "-pbkdf2",
        "-iter",
        "1",
        "-md",
        "sha256",
        "-pass",
        "env:ICTL_EK",
    ]
    if decrypt:
        args.append("-d")
    return subprocess.run(
        args,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=True,
        env={**os.environ, "ICTL_EK": ek},
    ).stdout


def mac(mk, data):
    return hmac.new(mk, data, hashlib.sha256).digest()


def seal_bytes(key, req_tag, data):
    ek, mk = derive(key)
    msg = aes(ek, data)[8:]
    return base64.b64encode(msg + mac(mk, req_tag + msg)).decode()


def seal(key, req_tag, text):
    return seal_bytes(key, req_tag, text.encode())
