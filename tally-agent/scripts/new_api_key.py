"""Generate an API key. Give the key to the person; store only the hash."""
import hashlib
import secrets

key = "tak_" + secrets.token_urlsafe(32)
print("API key (give to the user, shown once):", key)
print("key_sha256 (put in config/users.json):  ", hashlib.sha256(key.encode()).hexdigest())
