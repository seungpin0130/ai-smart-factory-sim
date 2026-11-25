"""Optional operator account authentication. Account files are provisioned offline."""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from backend.models import OperatorSession

COOKIE = "m16_operator"
ROLES = {"engineer", "maintenance", "manager"}
ITERATIONS = 600_000
SESSION_SECONDS = 8 * 3600


def accounts():
    configured = os.getenv("M16_OPERATOR_FILE")
    if not configured:
        return None
    entries = json.loads(Path(configured).read_text(encoding="utf-8"))
    if not entries or not isinstance(entries, dict):
        raise ValueError("M16_OPERATOR_FILE requires at least one account")
    for actor, entry in entries.items():
        salt, digest = entry["password_hash"].split(":")
        if (not actor.strip() or len(actor) > 64 or entry["role"] not in ROLES
                or len(bytes.fromhex(salt)) != 16 or len(bytes.fromhex(digest)) != 32):
            raise ValueError("Invalid operator account configuration")
    return entries


def password_hash(password, salt=None):
    salt = secrets.token_bytes(16) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify(entries, actor, password):
    entry = entries.get(actor)
    stored = entry["password_hash"] if entry else f'{"00" * 16}:{"00" * 32}'
    digest = password_hash(password, bytes.fromhex(stored.split(":")[0]))
    return hmac.compare_digest(stored, digest) and entry is not None


def token_key(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_session(db, actor):
    db.query(OperatorSession).filter(OperatorSession.expires_at <= int(time.time())).delete()
    token = secrets.token_urlsafe(32)
    db.add(OperatorSession(token_hash=token_key(token), actor_id=actor,
                           expires_at=int(time.time()) + SESSION_SECONDS))
    return token


def resolve_session(db, token, entries):
    if not token or len(token) > 100:
        return None
    row = db.get(OperatorSession, token_key(token))
    if row is None or row.expires_at <= int(time.time()) or row.actor_id not in entries:
        return None
    return row.actor_id, entries[row.actor_id]["role"]


if __name__ == "__main__":
    password = getpass.getpass("Operator password: ")
    if len(password) < 12:
        raise SystemExit("Use at least 12 characters.")
    print(password_hash(password))
