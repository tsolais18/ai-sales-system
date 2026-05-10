import os
import hashlib
from fastapi import Request, HTTPException

ADMIN_PASSWORD_HASH = hashlib.sha256(
    os.getenv("ADMIN_PASSWORD", "sell2025").encode()
).hexdigest()
# Never use "sell2025" in production – set the real password in Railway env vars.

def verify_password(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == ADMIN_PASSWORD_HASH

def login_required(request: Request):
    """Dependency that rejects unauthenticated users."""
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
