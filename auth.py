import os
import hashlib
from fastapi import Request, HTTPException, Depends
from supabase_client import supabase

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    return hash_password(password) == hashed

async def get_current_user(request: Request):
    """Return the logged-in user dict, or raise 401."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Fetch user from DB
    res = supabase.table("users").select("*").eq("id", user_id).single().execute()
    if not res.data:
        raise HTTPException(status_code=401, detail="User not found")
    return res.data

def login_required(request: Request):
    """Dependency for routes that just need authentication."""
    if not request.session.get("user_id"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    return True

