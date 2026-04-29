import os
import json
import asyncio
import logging
import re
from contextlib import asynccontextmanager

def escape_markdown(text: str) -> str:
    """Escape characters that Telegram's MarkdownV2 parser interprets."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

import telegram
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
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
        "Or just chat with me naturally — I got you! 😊"
    )


async def catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    products = get_all_products(business_id)
    if not products:
        await update.message.reply_text("😔 No products in stock right now. Check back soon!")
        return
    text = format_catalog(products)
    await update.message.reply_text(
        f"🛍 *Our Catalog*\n\n{text}\n\nTo order, just tell me the product name or ID!"
    )


async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /search <product name>")
        return
    products = search_products(query, business_id)
    if not products:
        await update.message.reply_text(f"😔 No results for *{query}*.")
        return
    text = format_catalog(products)
    await update.message.reply_text(text)


async def cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = view_cart(user_id)
    await update.message.reply_text(text)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    user_id = str(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /add <product_id> [quantity]\nExample: /add 3 2")
        return
    try:
        product_id = args[0]
        quantity = int(args[1]) if len(args) > 1 else 1
    except ValueError:
        await update.message.reply_text("❌ Invalid quantity. Use: /add <product_id> [quantity]")
        return
    reply = await add_to_cart(user_id, product_id, quantity, business_id)
    await update.message.reply_text(reply)


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from orders import get_orders_by_user, format_order_summary
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)
    user_id = str(update.effective_user.id)
    user_orders = get_orders_by_user(user_id, business_id)
    if not user_orders:
        await update.message.reply_text("📭 You have no orders yet.")
        return
    text = "\n\n".join([format_order_summary(o) for o in user_orders[:5]])
    await update.message.reply_text(text)


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
            await update.message.reply_text(reply)
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
templates.env.cache = None


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
    description: str = Form(""),   # optional – empty string is fine
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
        product = response.data[0]
        stock_badge = f'<span class="badge badge-in-stock">In Stock ({product["stock"]})</span>'
        # Return an HTML <tr> so HTMX can insert it into #product-list
        return HTMLResponse(f"""
        <tr id="product-{product['id']}">
            <td><span class="product-name">{product['name']}</span></td>
            <td><span class="amount">₦{product['price']:.2f}</span></td>
            <td>{stock_badge}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-btn action-edit"
                        hx-get="/admin/products/{product['id']}/edit-form"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">
                        Edit
                    </button>
                    <button class="action-btn action-delete"
                        hx-delete="/api/products/{product['id']}"
                        hx-confirm="Delete this product?"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">
                        Delete
                    </button>
                </div>
            </td>
        </tr>
        """, status_code=201)
    raise HTTPException(status_code=500, detail="Failed to add product")


# ── Update product (PATCH/PUT) ────────────────────────────
@app.patch("/api/products/{product_id}")
@app.put("/api/products/{product_id}")
async def api_update_product(product_id: str, request: Request):
    business_id = DEFAULT_BUSINESS_ID
    # Handle both JSON and form data (HTMX sends form-encoded)
    if request.headers.get("content-type") == "application/json":
        body = await request.json()
    else:
        form = await request.form()
        body = {k: v for k, v in form.items()}
        if "price" in body:
            body["price"] = float(body["price"])
        if "stock" in body:
            body["stock"] = int(body["stock"])

    res = supabase.table("products").update(body).eq("id", product_id).eq("business_id", business_id).execute()
    if res.data:
        product = res.data[0]
        stock_badge = f'<span class="badge badge-in-stock">In Stock ({product["stock"]})</span>' if product['stock'] > 0 else '<span class="badge badge-out">Out of Stock</span>'
        # Return the updated row as an HTML partial (matches _products.html style)
        return HTMLResponse(f"""
        <tr id="product-{product['id']}">
            <td><span class="product-name">{product['name']}</span></td>
            <td><span class="amount">₦{product['price']:.2f}</span></td>
            <td>{stock_badge}</td>
            <td>
                <div class="actions-cell">
                    <button class="action-btn action-edit"
                        hx-get="/admin/products/{product['id']}/edit-form"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">
                        Edit
                    </button>
                    <button class="action-btn action-delete"
                        hx-delete="/api/products/{product['id']}"
                        hx-confirm="Delete this product?"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">
                        Delete
                    </button>
                </div>
            </td>
        </tr>
        """)
    raise HTTPException(status_code=404, detail="Product not found")


# ── Delete product (DELETE) ──────────────────────────────
@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: str):
    business_id = DEFAULT_BUSINESS_ID
    resp = supabase.table("products") \
        .delete() \
        .eq("id", product_id) \
        .eq("business_id", business_id) \
        .execute()
    if resp.data:
        return Response(status_code=200)   # empty 200 tells HTMX to remove the element
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
        order = result[0]
        # Handle string items if necessary
        if isinstance(order.get("items"), str):
            order["items"] = json.loads(order["items"])
            
        items_html = "".join([f'<div>{i["name"]} <span style="color:var(--muted)">× {i["quantity"]}</span></div>' for i in order['items']])
        # Determine badge class
        badge_class = "badge-pending" if order['status'] == 'pending' else ("badge-confirmed" if order['status'] == 'confirmed' else "badge-cancelled")
        
        return HTMLResponse(f"""
        <tr id="order-{order['id']}">
            <td><span class="order-id">#{order['id']}</span></td>
            <td><span class="customer-name">{order['customer_name']}</span></td>
            <td><div class="item-list">{items_html}</div></td>
            <td><span class="amount">₦{order['total']:.2f}</span></td>
            <td><span class="badge {badge_class}">{order['status'].capitalize()}</span></td>
            <td>
                <div class="actions-cell">
                    <span style="color:var(--border);font-size:18px">—</span>
                </div>
            </td>
        </tr>
        """)
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
    template = templates.get_template("admin.html")
    content = template.render({"request": request})
    return HTMLResponse(content)


@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products_partial(request: Request):
    """Return only the product table (HTMX partial)."""
    business_id = DEFAULT_BUSINESS_ID  # will be replaced by real auth later
    products = supabase.table("products") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("id") \
        .execute()
    template = templates.get_template("_products.html")
    content = template.render({"request": request, "products": products.data or []})
    return HTMLResponse(content)


# ── Edit form (GET) ──────────────────────────────────────
@app.get("/admin/products/{product_id}/edit-form", response_class=HTMLResponse)
async def product_edit_form(product_id: str):
    res = supabase.table("products").select("*").eq("id", product_id).single().execute()
    product = res.data
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    return HTMLResponse(f"""
    <tr id="product-{product['id']}">
        <td><input type="text" name="name" value="{product['name']}" 
            style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:6px; width:100%; font-size:13px;"></td>
        <td><input type="number" name="price" step="0.01" value="{product['price']}" 
            style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:6px; width:90px; font-size:13px;"></td>
        <td><input type="number" name="stock" value="{product['stock']}" 
            style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:6px; width:70px; font-size:13px;"></td>
        <td>
            <div class="actions-cell">
                <button class="action-btn action-confirm"
                        hx-put="/api/products/{product['id']}" hx-include="closest tr"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">Save</button>
                <button class="action-btn action-delete"
                        hx-get="/admin/products/{product['id']}/view"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">Cancel</button>
            </div>
        </td>
    </tr>
    """)


# ── Cancel / View row (GET) ─────────────────────────────
@app.get("/admin/products/{product_id}/view", response_class=HTMLResponse)
async def product_view_row(product_id: str):
    res = supabase.table("products").select("*").eq("id", product_id).single().execute()
    product = res.data
    if not product:
        return HTMLResponse("", status_code=404)
    stock_badge = f'<span class="badge badge-in-stock">In Stock ({product["stock"]})</span>' if product['stock'] > 0 else '<span class="badge badge-out">Out of Stock</span>'
    return HTMLResponse(f"""
    <tr id="product-{product['id']}">
        <td><span class="product-name">{product['name']}</span></td>
        <td><span class="amount">₦{product['price']:.2f}</span></td>
        <td>{stock_badge}</td>
        <td>
            <div class="actions-cell">
                <button class="action-btn action-edit"
                        hx-get="/admin/products/{product['id']}/edit-form"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">Edit</button>
                <button class="action-btn action-delete"
                        hx-delete="/api/products/{product['id']}"
                        hx-confirm="Delete this product?"
                        hx-target="#product-{product['id']}" hx-swap="outerHTML">Delete</button>
            </div>
        </td>
    </tr>
    """)



@app.get("/admin/orders", response_class=HTMLResponse)
async def admin_orders_partial(request: Request, status: str = None):
    try:
        business_id = DEFAULT_BUSINESS_ID
        query = supabase.table("orders") \
            .select("*") \
            .eq("business_id", business_id) \
            .order("created_at", desc=True)
        if status:
            query = query.eq("status", status)

        orders_response = query.execute()
        raw_orders = orders_response.data or []

        # Convert items from JSON string to list if needed
        for order in raw_orders:
            if isinstance(order.get("items"), str):
                order["items"] = json.loads(order["items"])

        return templates.TemplateResponse("_orders.html", {
            "request": request,
            "orders": raw_orders
        })
    except Exception as e:
        logger.error(f"Orders page crash: {e}", exc_info=True)
        return HTMLResponse("<p>Something went wrong loading orders. Check logs.</p>", status_code=500)


# Override the old /api/products GET to return JSON (unchanged) –
# but for HTMX we'll use the /admin/products route above.
# The existing /api/products can remain as the pure JSON API.


@app.get("/admin/products/add-form", response_class=HTMLResponse)
async def product_add_form():
    return HTMLResponse("""
    <div style="background:var(--surface2); border:1px solid var(--border); padding:20px; border-radius:12px; margin-bottom:24px;">
        <h3 class="syne" style="font-weight:700; margin-bottom:16px; font-size:16px;">Add New Product</h3>
        <form hx-post="/api/products" hx-target="#product-list" hx-swap="afterbegin" hx-on::after-request="this.reset()">
            <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:12px; margin-bottom:16px;">
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <label style="font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase;">Name</label>
                    <input type="text" name="name" placeholder="e.g. Shoe Dog" 
                        style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; font-size:13px;" required>
                </div>
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <label style="font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase;">Description</label>
                    <input type="text" name="description" placeholder="Short description..." 
                        style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; font-size:13px;">
                </div>
                <div style="display:flex; flex-direction:column; gap:4px;">
                    <label style="font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase;">Price (₦)</label>
                    <input type="number" name="price" step="0.01" placeholder="0.00" 
                        style="background:var(--bg); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; font-size:13px;" required>
                </div>
            </div>
            <div style="display:flex; gap:8px;">
                <button type="submit" class="btn btn-primary">Create Product</button>
                <button type="button" class="btn btn-ghost" onclick="this.closest('div').parentElement.parentElement.remove()">Cancel</button>
            </div>
        </form>
    </div>
    """)


@app.get("/debug/orders-last")
async def debug_last_orders():
    # Fetch the last 5 orders directly from Supabase
    res = supabase.table("orders").select("*").order("created_at", desc=True).limit(5).execute()
    return res.data or []


@app.get("/debug/orders-raw")
async def debug_orders_raw():
    business_id = DEFAULT_BUSINESS_ID
    res = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("created_at", desc=True) \
        .execute()
    return {"count": len(res.data or []), "data": res.data}


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)