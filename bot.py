import os
import logging

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from groq import AsyncGroq
from catalog import get_all_products, get_product_by_id, get_product_by_name
from orders import create_order
from supabase_client import supabase
from dotenv import load_dotenv
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

load_dotenv()

groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))

ADMIN_IDS = [5851987998]

def validate_order_items(items: list, business_id: int) -> tuple:
    """
    Returns (valid_items, invalid_items) after checking product existence and stock.
    """
    valid = []
    invalid = []
    for item in items:
        # Try lookup by ID first (if it looks like a UUID), then by name
        product_ref = item["product_id"]
        product = None
        
        # Simple check: UUIDs are usually 36 chars with hyphens
        if len(product_ref) == 36 and "-" in product_ref:
            try:
                product = get_product_by_id(product_ref, business_id)
            except Exception:
                pass
        
        if not product:
            product = get_product_by_name(product_ref, business_id)

        if product and product.get("stock", 0) >= item.get("quantity", 1):
            # Store the resolved product in the item for Step 2
            item["_resolved_product"] = product
            valid.append(item)
        else:
            invalid.append(item)
            logger.warning(
                f"Invalid order item – ref={product_ref} "
                f"exists={bool(product)} stock={product.get('stock', 'N/A') if product else 'N/A'}"
            )
    return valid, invalid
sessions = {}

CUSTOMER_PROMPT = """
You are Cupa, a friendly AI sales assistant for an online store.
Keep ALL replies short — 2-4 sentences max. Sound warm and human, like a helpful friend.

STRICT ORDER FLOW — follow this exactly, step by step:
STEP 1: When customer wants to order, ask for their FULL NAME first. Nothing else.
STEP 2: After getting their name, ask for their DELIVERY ADDRESS. Nothing else.
STEP 3: After getting their address, confirm the order details and ask them to confirm.
STEP 4: Only after confirmation, output this EXACT line at the end of your reply:
##ORDER## customer_name | product_id:quantity,product_id:quantity | delivery_address

Example: ##ORDER## Amaka Obi | 3:1,5:2 | 14 Rumuola Road, Port Harcourt

NEVER skip steps. NEVER assume the address. ALWAYS ask explicitly.
NEVER output ##ORDER## without both name AND address.
After outputting ##ORDER## once, do NOT output it again. 
Never show ##ORDER## to the customer.

Also ensure the order format uses product names instead of IDs:
##ORDER## customer_name | product_name:quantity,... | address
Example: ##ORDER## Amaka Obi | Atomic Habits:1,Shoe Dog:2 | 14 Rumuola Road, Port Harcourt

Payment: GTBank — Store Account, Acct: 0123456789. Customer sends receipt here.
Delivery: 1-3 days locally, 3-5 days elsewhere.
Only reference products from the catalog. Never make up products.
"""

ADMIN_PROMPT = """
You are Cupa, AI business assistant for this online store.
You're talking to an admin. Be concise and direct.

You can:
- Answer questions about orders, revenue, inventory
- Help add new products conversationally
- Give stats and summaries

When admin wants to add a product, collect: name, description, price — then output:
##ADDPRODUCT## name | description | price
Example: ##ADDPRODUCT## Atomic Habits | Practical guide to building good habits | 5500

Always base answers on the business data provided.
"""


def get_session(user_id: str) -> dict:
    if user_id not in sessions:
        sessions[user_id] = {"history": [], "cart": [], "name": ""}
    return sessions[user_id]


def build_catalog_context(business_id: int) -> str:
    products = get_all_products(business_id)
    if not products:
        return "No products currently in stock."
    lines = [
        f"ID:{p['id']} | {p['name']} | {p['description']} | ₦{p['price']:,} | Stock: {p['stock']}"
        for p in products
    ]
    return "CURRENT CATALOG:\n" + "\n".join(lines)


def build_admin_data_context(business_id: int) -> str:
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    orders_res = supabase.table("orders").select("*").eq("business_id", business_id).order("created_at", desc=True).execute()
    all_orders = orders_res.data or []
    products_res = supabase.table("products").select("*").eq("business_id", business_id).execute()
    all_products = products_res.data or []

    today_orders = [o for o in all_orders if o["created_at"][:10] == today]
    pending = [o for o in all_orders if o["status"] == "pending"]
    confirmed = [o for o in all_orders if o["status"] == "confirmed"]
    this_month = now.strftime("%Y-%m")
    month_orders = [o for o in all_orders if o["created_at"][:7] == this_month]
    month_revenue = sum(o["total"] for o in month_orders if o["status"] == "confirmed")
    today_revenue = sum(o["total"] for o in today_orders if o["status"] == "confirmed")
    total_revenue = sum(o["total"] for o in all_orders if o["status"] == "confirmed")

    product_counts = {}
    for order in all_orders:
        for item in order.get("items", []):
            name = item.get("name", "Unknown")
            product_counts[name] = product_counts.get(name, 0) + item.get("quantity", 1)
    top_products = sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    recent = all_orders[:5]
    recent_lines = [
        f"  #{o['id']} | {o['customer_name']} | {o.get('location', 'N/A')} | ₦{o['total']:,} | {o['status']}"
        for o in recent
    ]

    in_stock_count = len([p for p in all_products if p["stock"] > 0])
    out_of_stock_count = len([p for p in all_products if p["stock"] == 0])

    return f"""
BUSINESS DATA ({now.strftime('%Y-%m-%d %H:%M')} UTC):
Orders today: {len(today_orders)} | This month: {len(month_orders)} | Pending: {len(pending)} | Confirmed: {len(confirmed)}
Revenue today: ₦{today_revenue:,} | This month: ₦{month_revenue:,} | All time: ₦{total_revenue:,}
Inventory: {in_stock_count} in stock, {out_of_stock_count} out of stock
Top products: {', '.join([f"{t}({c})" for t, c in top_products]) or 'none yet'}
Recent orders:
{chr(10).join(recent_lines) or '  None yet'}
"""


def parse_order_signal(reply: str):
    for line in reply.split("\n"):
        if line.strip().startswith("##ORDER##"):
            try:
                data = line.replace("##ORDER##", "").strip()
                parts = [p.strip() for p in data.split("|")]
                customer_name = parts[0]
                items = []
                for item_str in parts[1].strip().split(","):
                    product_id, quantity = item_str.strip().split(":")
                    items.append({"product_id": product_id.strip(), "quantity": int(quantity)})
                location = parts[2] if len(parts) > 2 else None
                return customer_name, items, location
            except Exception:
                return None, None, None
    return None, None, None


def parse_addproduct_signal(reply: str):
    for line in reply.split("\n"):
        if line.strip().startswith("##ADDPRODUCT##"):
            try:
                data = line.replace("##ADDPRODUCT##", "").strip()
                parts = [p.strip() for p in data.split("|")]
                return {
                    "name": parts[0],
                    "description": parts[1],
                    "price": float(parts[2].replace(",", "").replace("₦", "")),
                    "stock": 1,
                }
            except Exception:
                return None
    return None


async def handle_message(user_id: str, user_message: str, bot=None, business_id: int = 1) -> str:
    session = get_session(user_id)
    is_admin_user = int(user_id) in ADMIN_IDS

    if is_admin_user:
        return await handle_admin_message(user_id, user_message, session, bot, business_id)
    else:
        return await handle_customer_message(user_id, user_message, session, bot, business_id)


async def handle_admin_message(user_id: str, user_message: str, session: dict, bot=None, business_id: int = 1) -> str:
    admin_data = build_admin_data_context(business_id)
    catalog_context = build_catalog_context(business_id)
    admin_key = f"admin_{user_id}"
    if admin_key not in sessions:
        sessions[admin_key] = {"history": []}
    admin_session = sessions[admin_key]
    admin_session["history"].append({"role": "user", "content": user_message})

    messages = [
        {"role": "system", "content": f"{ADMIN_PROMPT}\n\n{admin_data}\n\n{catalog_context}"},
        *admin_session["history"][-10:],
    ]
    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile", messages=messages, temperature=0.3, max_tokens=600,
    )
    reply = response.choices[0].message.content.strip()
    admin_session["history"].append({"role": "assistant", "content": reply})

    product_data = parse_addproduct_signal(reply)
    if product_data:
        product_data["business_id"] = business_id
        res = supabase.table("products").insert(product_data).execute()
        clean_reply = "\n".join(
            l for l in reply.split("\n") if not l.strip().startswith("##ADDPRODUCT##")
        ).strip()
        if res.data:
            return clean_reply + f"\n\n✅ *{product_data['name']}* added to catalog! ID: `{res.data[0]['id']}`"
        return clean_reply + "\n\n❌ Failed to add product."

    return reply


async def handle_customer_message(user_id: str, user_message: str, session: dict, bot=None, business_id: int = 1) -> str:
    catalog_context = build_catalog_context(business_id)
    session["history"].append({"role": "user", "content": user_message})

    messages = [
        {"role": "system", "content": f"{CUSTOMER_PROMPT}\n\n{catalog_context}"},
        *session["history"][-10:],
    ]
    response = await groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile", messages=messages, temperature=0.7, max_tokens=400,
    )
    reply = response.choices[0].message.content.strip()
    session["history"].append({"role": "assistant", "content": reply})

    customer_name, order_items, location = parse_order_signal(reply)
    if customer_name and order_items and location:
        await save_order(user_id, customer_name, order_items, bot, location, business_id)
        clean_reply = "\n".join(
            l for l in reply.split("\n") if not l.strip().startswith("##ORDER##")
        ).strip()
        # Prevent sending an empty message to Telegram
        if not clean_reply:
            clean_reply = "✅ Your order has been placed! We'll send you an update once it's confirmed."
        return clean_reply

    return reply


async def save_order(
    user_id: str, customer_name: str, items: list, bot=None,
    location: str = "Not provided", business_id: int = 1
):
    # Step 1 – Validate every item
    valid_items, invalid_items = validate_order_items(items, business_id)
    if invalid_items:
        logger.info(f"Ignored {len(invalid_items)} hallucinated items from user {user_id}")

    if not valid_items:
        logger.error(f"No valid products in order from user {user_id} – order discarded")
        if bot:
            try:
                await bot.send_message(
                    chat_id=int(user_id),
                    text="❌ Sorry, some items in your order are no longer available. Please check the catalog and try again."
                )
            except Exception:
                pass
        return None

    # Step 2 – Enrich items with product details
    enriched_items = []
    total = 0
    for item in valid_items:
        product = item.get("_resolved_product")
        if not product: # Fallback just in case
            product = get_product_by_name(item["product_id"], business_id) or get_product_by_id(item["product_id"], business_id)
        
        if product:
            enriched_items.append({
                "product_id": product["id"],
                "name": product["name"],
                "quantity": item.get("quantity", 1),
                "price": product["price"],
            })
            total += product["price"] * item.get("quantity", 1)

    # Step 3 – Create the order
    order = create_order(
        customer_name=customer_name,
        telegram_id=user_id,
        items=enriched_items,
        total=total,
        location=location,
        business_id=business_id,
    )
    
    if order:
        logger.info(f"Order created: {order['id']} for business {business_id}, total ₦{total:,}")

    if order and bot:
        short_id = order['id'][:8]
        # Notify admins
        items_text = "\n".join([f"  • {i['name']} x{i['quantity']} — ₦{i['price']:,}" for i in enriched_items])
        admin_msg_clean = (
            f"🛎 *New Order #{short_id}!*\n\n"
            f"👤 *{customer_name}*\n"
            f"📍 *{location}*\n\n"
            f"{items_text}\n\n"
            f"💰 Total: ₦{total:,}"
        )
        
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{order['id']}")],
             [InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{order['id']}")]]
        )

        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_msg_clean,
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    return order


async def add_to_cart(user_id: str, product_id: str, quantity: int = 1, business_id: int = 1) -> str:
    session = get_session(user_id)
    product = get_product_by_id(product_id, business_id)
    if not product:
        return f"❌ Product with ID {product_id} not found."
    for item in session["cart"]:
        if item["product_id"] == product_id:
            item["quantity"] += quantity
            return f"✅ Updated cart: *{product['name']}* x{item['quantity']}"
    session["cart"].append({
        "product_id": product["id"],
        "name": product["name"],
        "quantity": quantity,
        "price": product["price"],
    })
    return f"✅ Added to cart: *{product['name']}* — ₦{product['price']:,}"


def view_cart(user_id: str) -> str:
    session = get_session(user_id)
    cart = session.get("cart", [])
    if not cart:
        return "🛒 Your cart is empty."
    lines = [f"  • {i['name']} x{i['quantity']} — ₦{i['price'] * i['quantity']:,}" for i in cart]
    total = sum(i["price"] * i["quantity"] for i in cart)
    return "🛒 *Your Cart:*\n" + "\n".join(lines) + f"\n\n💰 Total: ₦{total:,}"