import os
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from supabase_client import supabase
from orders import get_orders_by_user, format_order_summary

ADMIN_IDS = [5851987998]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ You don't have permission to use this command.")
            return
        return await func(update, context)
    return wrapper


# ── /admin ────────────────────────────────────────────────
@admin_only
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛠 *Admin Panel*\n\n"
        "*Inventory:*\n"
        "• `/addproduct <name> | <description> | <price>` — Add a product\n"
        "• `/outofstock <id>` — Set stock to 0\n"
        "• `/restock <id>` — Set stock back to 1\n"
        "• `/setstock <id> <qty>` — Set exact stock quantity\n"
        "• `/deleteproduct <id>` — Delete a product permanently\n"
        "• `/products` — List all products (including out of stock)\n\n"
        "*Orders:*\n"
        "• `/pending` — View all pending orders\n"
        "• `/confirm <order_id>` — Confirm a manual payment\n"
        "• `/cancelorder <order_id>` — Cancel an order\n",
        parse_mode="Markdown"
    )


# ── /addproduct ───────────────────────────────────────────
@admin_only
async def add_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        await update.message.reply_text(
            "❌ Wrong format.\n"
            "Usage: `/addproduct Name | Description | Price`\n"
            "Example: `/addproduct Shoe Dog | Nike founder memoir | 6500`",
            parse_mode="Markdown"
        )
        return

    name, description, price_str = parts
    try:
        price = float(price_str.replace(",", "").replace("₦", "").strip())
    except ValueError:
        await update.message.reply_text("❌ Invalid price. Use numbers only, e.g. `6500`", parse_mode="Markdown")
        return

    response = supabase.table("products").insert({
        "name": name,
        "description": description,
        "price": price,
        "stock": 1,
        "business_id": business_id,
    }).execute()

    if response.data:
        product = response.data[0]
        await update.message.reply_text(
            f"✅ Product added!\n\n"
            f"📦 *{product['name']}*\n"
            f"📝 {product['description']}\n"
            f"💰 ₦{product['price']:,.0f}\n"
            f"📦 Stock: {product['stock']}\n"
            f"🆔 ID: `{product['id']}`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Failed to add product. Try again.")


# ── /outofstock ───────────────────────────────────────────
@admin_only
async def out_of_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if not context.args:
        await update.message.reply_text("Usage: `/outofstock <product_id>`", parse_mode="Markdown")
        return
    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid product ID.")
        return

    response = supabase.table("products").update({"stock": 0}).eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        await update.message.reply_text(f"✅ Product ID `{product_id}` marked as out of stock (stock = 0).", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Product ID `{product_id}` not found.")


# ── /restock ──────────────────────────────────────────────
@admin_only
async def restock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if not context.args:
        await update.message.reply_text("Usage: `/restock <product_id>`", parse_mode="Markdown")
        return
    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid product ID.")
        return

    response = supabase.table("products").update({"stock": 1}).eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        await update.message.reply_text(f"✅ Product ID `{product_id}` restocked (stock = 1).", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Product ID `{product_id}` not found.")


# ── /setstock ─────────────────────────────────────────────
@admin_only
async def set_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/setstock <product_id> <quantity>`", parse_mode="Markdown")
        return
    try:
        product_id = int(context.args[0])
        quantity = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Invalid product ID or quantity.")
        return

    response = supabase.table("products").update({"stock": quantity}).eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        await update.message.reply_text(f"✅ Product ID `{product_id}` stock set to {quantity}.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Product ID `{product_id}` not found.")


# ── /deleteproduct ────────────────────────────────────────
@admin_only
async def delete_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if not context.args:
        await update.message.reply_text("Usage: `/deleteproduct <product_id>`", parse_mode="Markdown")
        return
    try:
        product_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid product ID.")
        return

    response = supabase.table("products").delete().eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        await update.message.reply_text(f"🗑 Product ID `{product_id}` deleted permanently.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Product ID `{product_id}` not found.")


# ── /products (admin view) ────────────────────────────────
@admin_only
async def admin_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    response = supabase.table("products").select("*").eq("business_id", business_id).order("id").execute()
    products = response.data or []
    if not products:
        await update.message.reply_text("No products in the database.")
        return

    lines = []
    for p in products:
        status = "✅" if p["stock"] > 0 else "❌"
        lines.append(f"{status} `{p['id']}` | *{p['name']}* — ₦{p['price']:,.0f} | Stock: {p['stock']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /pending ──────────────────────────────────────────────
@admin_only
async def pending_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    response = (
        supabase.table("orders")
        .select("*")
        .eq("business_id", business_id)
        .eq("status", "pending")
        .order("created_at", desc=True)
        .execute()
    )
    orders = response.data or []
    if not orders:
        await update.message.reply_text("📭 No pending orders.")
        return

    for order in orders[:10]:
        items_text = "\n".join(
            [f"  • {i['name']} x{i['quantity']} — ₦{i['price']:,}" for i in order["items"]]
        )
        text = (
            f"🧾 *Order #{order['id']}*\n"
            f"👤 {order['customer_name']} (TG: `{order['telegram_id']}`)\n"
            f"{items_text}\n"
            f"💰 Total: ₦{order['total']:,}\n"
            f"🕐 {order['created_at'][:16]}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")


# ── /confirm ──────────────────────────────────────────────
@admin_only
async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if not context.args:
        await update.message.reply_text("Usage: `/confirm <order_id>`", parse_mode="Markdown")
        return
    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid order ID.")
        return

    response = supabase.table("orders").update({"status": "confirmed"}).eq("id", order_id).eq("business_id", business_id).execute()
    if response.data:
        order = response.data[0]
        await update.message.reply_text(
            f"✅ Order #{order_id} confirmed!\n"
            f"Customer TG ID: `{order['telegram_id']}`",
            parse_mode="Markdown"
        )
        try:
            await context.bot.send_message(
                chat_id=int(order["telegram_id"]),
                text=f"🎉 Your order #{order_id} has been confirmed! We'll process it right away. Thank you for shopping with us! 🛍"
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(f"❌ Order #{order_id} not found.")


# ── /cancelorder ──────────────────────────────────────────
@admin_only
async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", 1)
    if not context.args:
        await update.message.reply_text("Usage: `/cancelorder <order_id>`", parse_mode="Markdown")
        return
    try:
        order_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid order ID.")
        return

    response = supabase.table("orders").update({"status": "cancelled"}).eq("id", order_id).eq("business_id", business_id).execute()
    if response.data:
        await update.message.reply_text(f"🚫 Order #{order_id} cancelled.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Order #{order_id} not found.")


# ── Register all admin handlers ───────────────────────────
def register_admin_handlers(app):
    app.add_handler(CommandHandler("admin", admin_menu))
    app.add_handler(CommandHandler("addproduct", add_product))
    app.add_handler(CommandHandler("outofstock", out_of_stock))
    app.add_handler(CommandHandler("restock", restock))
    app.add_handler(CommandHandler("setstock", set_stock))
    app.add_handler(CommandHandler("deleteproduct", delete_product))
    app.add_handler(CommandHandler("products", admin_list_products))
    app.add_handler(CommandHandler("pending", pending_orders))
    app.add_handler(CommandHandler("confirm", confirm_order))
    app.add_handler(CommandHandler("cancelorder", cancel_order))