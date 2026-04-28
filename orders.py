from supabase_client import supabase
from datetime import datetime


def create_order(customer_name: str, telegram_id: str, items: list, total: float, location: str = "Not provided"):
    """
    items: [{"product_id": 1, "name": "...", "quantity": 1, "price": 5000}]
    """
    order = {
        "customer_name": customer_name,
        "telegram_id": str(telegram_id),
        "items": items,
        "total": total,
        "status": "pending",
        "location": location,
        "created_at": datetime.utcnow().isoformat(),
    }
    response = supabase.table("orders").insert(order).execute()
    return response.data[0] if response.data else None


def get_orders_by_user(telegram_id: str):
    response = (
        supabase.table("orders")
        .select("*")
        .eq("telegram_id", str(telegram_id))
        .order("created_at", desc=True)
        .execute()
    )
    return response.data or []


def get_all_orders(status: str = None):
    query = supabase.table("orders").select("*").order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    return query.execute().data or []


def update_order_status(order_id: int, status: str):
    response = (
        supabase.table("orders")
        .update({"status": status})
        .eq("id", order_id)
        .execute()
    )
    return response.data


def format_order_summary(order: dict) -> str:
    items_text = "\n".join(
        [f"  • {i['name']} x{i['quantity']} — ₦{i['price']:,}" for i in order["items"]]
    )
    return (
        f"🧾 *Order #{order['id']}*\n"
        f"{items_text}\n"
        f"💰 Total: ₦{order['total']:,}\n"
        f"📦 Status: {order['status'].capitalize()}"
    )