import os
import asyncio
import logging
from contextlib import asynccontextmanager

import telegram
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

from bot import handle_message, add_to_cart, view_cart
from catalog import get_all_products, search_products, format_catalog
from admin import register_admin_handlers
from orders import get_all_orders, update_order_status
from supabase_client import supabase

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Telegram Bot Setup ────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_BUSINESS_ID = 1

telegram_app: Application = None


async def get_business_id_for_telegram(token: str) -> int:
    """Fetch the business_id linked to this Telegram bot token."""
    try:
        res = supabase.table("channels") \
            .select("business_id") \
            .eq("channel_type", "telegram") \
            .eq("identifier", token) \
            .limit(1) \
            .execute()
        if res.data and len(res.data) > 0:
            return res.data[0]["business_id"]
    except Exception as e:
        logger.error(f"Failed to resolve business_id for token: {e}")
    return DEFAULT_BUSINESS_ID


# ── Telegram command handlers ─────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Hey {name}! Welcome to our store 🛍\n\n"
        "I'm Cupa, your personal shopping assistant.\n\n"
        "Here's what I can do:\n"
        "• /catalog — Browse all products\n"
        "• /search <name> — Find a product\n"
        "• /cart — View your cart\n"
        "• /orders — Your order history\n\n"
        "Or just chat with me naturally — I got you! 😊",
        parse_mode="Markdown"
    )


async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    products = get_all_products(business_id)
    if not products:
        await update.message.reply_text("😔 No products in stock right now. Check back soon!")
        return
    text = format_catalog(products)
    await update.message.reply_text(
        f"🛍 *Our Catalog*\n\n{text}\n\nTo order, just tell me the product name or ID!",
        parse_mode="Markdown"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search <product name>")
        return
    products = search_products(query, business_id)
    if not products:
        await update.message.reply_text(f"😔 No results for *{query}*.", parse_mode="Markdown")
        return
    text = format_catalog(products)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = view_cart(user_id)
    await update.message.reply_text(text, parse_mode="Markdown")


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    user_id = str(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /add <product_id> [quantity]\nExample: /add 3 2")
        return
    try:
        product_id = int(args[0])
        quantity = int(args[1]) if len(args) > 1 else 1
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use: /add <product_id> [quantity]")
        return
    reply = await add_to_cart(user_id, product_id, quantity, business_id)
    await update.message.reply_text(reply, parse_mode="Markdown")


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from orders import get_orders_by_user, format_order_summary
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    user_id = str(update.effective_user.id)
    user_orders = get_orders_by_user(user_id, business_id)
    if not user_orders:
        await update.message.reply_text("📭 You have no orders yet.")
        return
    text = "\n\n".join([format_order_summary(o) for o in user_orders[:5]])
    await update.message.reply_text(text, parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    user_id = str(update.effective_user.id)
    user_message = update.message.text

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except telegram.error.NetworkError as e:
        logger.debug(f"Could not send chat action: {e}")

    reply = await handle_message(user_id, user_message, bot=context.bot, business_id=business_id)

    for attempt in range(3):
        try:
            await update.message.reply_text(reply, parse_mode="Markdown")
            break
        except telegram.error.NetworkError as e:
            logger.warning(f"Network error on reply attempt {attempt+1}/3: {e}")
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
            else:
                logger.error("Failed to send reply after 3 attempts")
                await update.message.reply_text("❌ Sorry, a network issue occurred. Please try again.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, telegram.error.NetworkError):
        logger.warning(f"⚠️ Network issue – will retry on next update: {context.error}")
        if update and hasattr(update, 'effective_message'):
            try:
                await update.effective_message.reply_text("❌ Connection issue, please try again.")
            except Exception:
                pass
    else:
        logger.error(f"Unhandled error: {context.error}", exc_info=context.error)


def build_telegram_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    app = Application.builder().token(token).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("catalog", catalog))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("cart", cart))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    register_admin_handlers(app)

    app.add_error_handler(error_handler)
    return app


# ── FastAPI App + Lifespan ────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    try:
        telegram_app = build_telegram_app()
        await telegram_app.initialize()
        await telegram_app.start()

        # Resolve business_id and store in bot_data
        bid = await get_business_id_for_telegram(TELEGRAM_BOT_TOKEN)
        telegram_app.bot_data["business_id"] = bid
        logger.info(f"🤖 Telegram bot started for business_id={bid}")

        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
        await telegram_app.updater.start_polling()
    except Exception as e:
        logger.error(f"❌ Failed to start Telegram bot: {e}")

    yield

    if telegram_app:
        if telegram_app.updater.running:
            await telegram_app.updater.stop()
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("🤖 Telegram bot stopped")


app = FastAPI(title="Store Admin API", lifespan=lifespan)


@app.get("/debug/catalog")
async def debug_catalog():
    business_id = DEFAULT_BUSINESS_ID
    products = supabase.table("products") \
        .select("*") \
        .eq("business_id", business_id) \
        .gt("stock", 0) \
        .execute()
    return {
        "business_id": business_id,
        "count": len(products.data or []),
        "products": products.data
    }


# Static files with safety check
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
else:
    logger.warning("⚠️ 'static' directory not found — skipping static file mount.")

templates = Jinja2Templates(directory="templates")


# ── Web Admin Routes ──────────────────────────────────────
# Note: API routes currently use DEFAULT_BUSINESS_ID until proper auth is added.




@app.get("/api/products")
async def api_get_products():
    business_id = DEFAULT_BUSINESS_ID  # TODO: get from authenticated user's business
    response = supabase.table("products").select("*").eq("business_id", business_id).order("id").execute()
    return response.data or []


@app.post("/api/products")
async def api_add_product(
    name: str = Form(...),
    description: str = Form(...),
    price: float = Form(...),
):
    business_id = DEFAULT_BUSINESS_ID
    response = supabase.table("products").insert({
        "name": name,
        "description": description,
        "price": price,
        "stock": 1,
        "business_id": business_id,
    }).execute()
    if response.data:
        return JSONResponse(status_code=201, content=response.data[0])
    raise HTTPException(status_code=500, detail="Failed to add product")


@app.patch("/api/products/{product_id}")
async def api_update_product(product_id: int, request: Request):
    business_id = DEFAULT_BUSINESS_ID
    body = await request.json()
    response = supabase.table("products").update(body).eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        return response.data[0]
    raise HTTPException(status_code=404, detail="Product not found")


@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: int):
    business_id = DEFAULT_BUSINESS_ID
    response = supabase.table("products").delete().eq("id", product_id).eq("business_id", business_id).execute()
    if response.data:
        return {"success": True}
    raise HTTPException(status_code=404, detail="Product not found")


@app.get("/api/orders")
async def api_get_orders(status: str = None):
    business_id = DEFAULT_BUSINESS_ID
    return get_all_orders(business_id, status=status)


@app.patch("/api/orders/{order_id}/status")
async def api_update_order_status(order_id: int, request: Request):
    business_id = DEFAULT_BUSINESS_ID
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "confirmed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    result = update_order_status(order_id, business_id, new_status)
    if result:
        return result[0]
    raise HTTPException(status_code=404, detail="Order not found")


@app.get("/api/stats")
async def api_stats():
    business_id = DEFAULT_BUSINESS_ID
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    orders_res = supabase.table("orders").select("*").eq("business_id", business_id).execute()
    all_orders = orders_res.data or []
    products_res = supabase.table("products").select("*").eq("business_id", business_id).execute()
    all_products = products_res.data or []

    today_orders = [o for o in all_orders if o["created_at"][:10] == today]
    month_orders = [o for o in all_orders if o["created_at"][:7] == this_month]
    confirmed = [o for o in all_orders if o["status"] == "confirmed"]

    return {
        "total_products": len(all_products),
        "in_stock": len([p for p in all_products if p["stock"] > 0]),
        "total_orders": len(all_orders),
        "pending_orders": len([o for o in all_orders if o["status"] == "pending"]),
        "today_orders": len(today_orders),
        "month_revenue": sum(o["total"] for o in month_orders if o["status"] == "confirmed"),
        "total_revenue": sum(o["total"] for o in confirmed),
    }


# ── Web Admin Dashboard Routes ────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Render the full admin dashboard page."""
    return templates.TemplateResponse("admin.html", {"request": request})


@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products_partial(request: Request):
    """Return only the product table (HTMX partial)."""
    business_id = DEFAULT_BUSINESS_ID  # will be replaced by real auth later
    products = supabase.table("products") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("id") \
        .execute()
    return templates.TemplateResponse("_products.html", {
        "request": request,
        "products": products.data or []
    })


@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders_partial(request: Request, status: str = None):
    """Return only the orders table (HTMX partial)."""
    business_id = DEFAULT_BUSINESS_ID
    query = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("created_at", desc=True)
    if status:
        query = query.eq("status", status)
    orders = query.execute()
    return templates.TemplateResponse("_orders.html", {
        "request": request,
        "orders": orders.data or []
    })


# Override the old /api/products GET to return JSON (unchanged) –
# but for HTMX we'll use the /admin/products route above.
# The existing /api/products can remain as the pure JSON API.


@app.get("/admin/products/add-form", response_class=HTMLResponse)
async def product_add_form():
    return HTMLResponse("""
    <form hx-post="/api/products" hx-target="#product-list" hx-swap="beforeend"
          class="p-4 bg-white border border-gray-200 rounded-xl shadow-sm max-w-lg">
        <h3 class="text-lg font-semibold mb-4">New Product</h3>
        <div class="space-y-3">
            <input type="text" name="name" placeholder="Product name" required
                   class="w-full border border-gray-300 rounded-lg px-4 py-2 text-sm focus:ring-2 focus:ring-purple-500 focus:border-purple-500 outline-none">
            <textarea name="description" placeholder="Description" required rows="2"
                      class="w-full border border-gray-300 rounded-lg px-4 py-2 text-sm focus:ring-2 focus:ring-purple-500 focus:border-purple-500 outline-none"></textarea>
            <input type="number" name="price" step="0.01" placeholder="Price (₦)" required
                   class="w-full border border-gray-300 rounded-lg px-4 py-2 text-sm focus:ring-2 focus:ring-purple-500 focus:border-purple-500 outline-none">
        </div>
        <div class="mt-4 flex gap-3">
            <button type="submit" class="bg-purple-600 hover:bg-purple-700 text-white px-5 py-2 rounded-lg text-sm font-medium transition-colors">Save Product</button>
            <button type="button" class="text-gray-500 hover:text-gray-700 text-sm"
                    hx-get="/admin/products" hx-target="#product-list">Cancel</button>
        </div>
    </form>
    """)


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)