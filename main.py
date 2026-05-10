import os
import json
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

def escape_markdown(text: str) -> str:
    """Escape characters that Telegram's MarkdownV2 parser interprets."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

import telegram
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, HTTPException, Header
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import RedirectResponse
from fastapi import Depends
from auth import get_current_user, hash_password, verify_password, login_required


from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)
from telegram.request import HTTPXRequest

from bot import handle_message, add_to_cart, view_cart
from catalog import get_all_products, search_products, format_catalog
from admin import register_admin_handlers
from orders import get_all_orders, update_order_status
from supabase_client import supabase

load_dotenv()

TOKEN_FILTER = re.compile(r"(bot\d+:)[\w\-]+")

class TokenMaskingFilter(logging.Filter):
    def filter(self, record):
        if record.msg and isinstance(record.msg, str):
            record.msg = TOKEN_FILTER.sub(r"\1***", record.msg)
        return True

async def csrf_check(request: Request):
    # Bypass CSRF for GET requests and login/logout
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.url.path in ("/login", "/logout"):
        return
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="CSRF token missing")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").addFilter(TokenMaskingFilter())

# ── Telegram Bot Setup ────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_BUSINESS_ID = 1

def get_current_business_id(request: Request) -> int:
    """Get business_id from session or from the logged-in user."""
    # First check if a specific business is selected (for superadmin switched)
    selected = request.session.get("current_business_id")
    if selected:
        return selected

    # Fall back to the user's own business
    user_id = request.session.get("user_id")
    if user_id:
        user_res = supabase.table("users").select("business_id", "is_superadmin").eq("id", user_id).single().execute()
        if user_res.data:
            if user_res.data["is_superadmin"]:
                return DEFAULT_BUSINESS_ID   # superadmin sees default, but can switch
            return user_res.data.get("business_id") or DEFAULT_BUSINESS_ID
    return DEFAULT_BUSINESS_ID

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


# ── App & Security Setup ──────────────────────────────────


# ── App & Security Setup ──────────────────────────────────

async def build_telegram_app() -> Application:
    # Fetch any active Telegram token (just for initialization)
    res = supabase.table("channels") \
        .select("identifier") \
        .eq("channel_type", "telegram") \
        .eq("is_active", True) \
        .limit(1) \
        .execute()
    
    token = res.data[0]["identifier"] if res.data and len(res.data) > 0 else os.getenv("FALLBACK_BOT_TOKEN", "none")

    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    
    app_builder = Application.builder().token(token).request(request)
    application = app_builder.build()
    
    # Core command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("catalog", catalog))
    application.add_handler(CommandHandler("search", search))
    application.add_handler(CommandHandler("cart", cart))
    application.add_handler(CommandHandler("add", add))
    application.add_handler(CommandHandler("orders", orders_cmd))
    application.add_handler(CallbackQueryHandler(handle_order_callback, pattern="^(confirm|cancel):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    
    # Register admin commands
    register_admin_handlers(application)
    
    application.add_error_handler(error_handler)
    return application

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Setup Telegram bots for all active channels."""
    global telegram_app
    try:
        # 1. Initialize the Telegram Application
        telegram_app = await build_telegram_app()
        await telegram_app.initialize()
        await telegram_app.start()

        # 2. Set webhooks for all businesses
        base_url = os.getenv("RAILWAY_PUBLIC_URL", "https://your-app.up.railway.app")
        channels = supabase.table("channels") \
            .select("*") \
            .eq("channel_type", "telegram") \
            .eq("is_active", True) \
            .execute()

        for ch in channels.data or []:
            tok = ch["identifier"]
            try:
                bot = telegram.Bot(tok)
                await bot.set_webhook(f"{base_url}/webhook/{tok}")
                logger.info(f"Webhook set for channel {ch['id']} (business {ch['business_id']})")
            except Exception as e:
                logger.error(f"Failed to set webhook for channel {ch['id']}: {e}")

        logger.info("🤖 Telegram bots running in webhook mode")
    except Exception as e:
        logger.error(f"❌ Failed to start Telegram bot: {e}")

    yield

    if telegram_app:
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("🤖 Telegram bot stopped")

app = FastAPI(title="Sell! Admin API", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "change-me-now"))



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


async def handle_order_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # "confirm:<order_id>" or "cancel:<order_id>"
    action, order_id = data.split(":", 1)
    business_id = context.bot_data.get("business_id", DEFAULT_BUSINESS_ID)

    if action == "confirm":
        new_status = "confirmed"
    elif action == "cancel":
        new_status = "cancelled"
    else:
        return

    from orders import update_order_status
    result = update_order_status(order_id, business_id, new_status)

    if result:
        order = result[0]
        try:
            # Notify the customer
            await context.bot.send_message(
                chat_id=int(order["telegram_id"]),
                text=f"📢 Your order #{order['id'][:8]} has been {new_status}. Thank you!"
            )
        except Exception as e:
            logger.warning(f"Failed to notify customer {order['telegram_id']}: {e}")
        
        # Update the admin message to show the final status
        current_text = query.message.text
        status_emoji = "✅" if new_status == "confirmed" else "🚫"
        # Append status if not already there
        if f"Status: {new_status.capitalize()}" not in current_text:
            await query.edit_message_text(
                text=f"{current_text}\n\n{status_emoji} *Status: {new_status.capitalize()}*",
                parse_mode="Markdown"
            )
    else:
        await query.edit_message_text(
            text=f"{query.message.text}\n\n⚠️ Failed to update order status."
        )


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








@app.get("/debug/catalog")
async def debug_catalog(request: Request):
    business_id = get_current_business_id(request)
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





# ── Authentication ──────────────────────────────────────
@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    template = templates.get_template("signup.html")
    return HTMLResponse(template.render({"request": request}))

@app.post("/signup")
async def signup(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    business_name: str = Form(...)
):
    # Basic validation
    if len(password) < 6:
        return HTMLResponse("Password must be at least 6 characters", status_code=400)
    if "@" not in email:
        return HTMLResponse("Invalid email address", status_code=400)

    # Check if email already exists
    existing = supabase.table("users").select("id").eq("email", email).execute()
    if existing.data:
        return HTMLResponse("An account with this email already exists", status_code=400)

    # Create the business
    slug = business_name.lower().replace(" ", "-")
    biz_res = supabase.table("businesses").insert({
        "name": business_name,
        "slug": slug,
    }).execute()
    business_id = biz_res.data[0]["id"]

    # Create the user
    password_hash = hash_password(password)   # from auth.py
    supabase.table("users").insert({
        "email": email,
        "password_hash": password_hash,
        "business_id": business_id,
        "is_superadmin": False
    }).execute()

    # Fetch the new user to get its ID
    user_res = supabase.table("users").select("id").eq("email", email).single().execute()
    request.session["user_id"] = user_res.data["id"]

    return RedirectResponse("/admin", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    template = templates.get_template("login.html")
    return HTMLResponse(template.render({"request": request}))

@app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...)):
    # Look up user by email
    res = supabase.table("users").select("*").eq("email", email).single().execute()
    if not res.data or not verify_password(password, res.data["password_hash"]):
        return HTMLResponse("Invalid email or password", status_code=401)
    user = res.data
    request.session["user_id"] = user["id"]
    return RedirectResponse("/admin", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/api/products")

async def api_get_products(request: Request):
    business_id = get_current_business_id(request)  # TODO: get from authenticated user's business
    response = supabase.table("products").select("*").eq("business_id", business_id).order("id").execute()
    return response.data or []


@app.post("/api/products")
async def api_add_product(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    price: float = Form(...),
    _=Depends(login_required),
    _csrf=Depends(csrf_check)
):
    business_id = get_current_business_id(request)
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
async def api_update_product(product_id: str, request: Request, hx_target: str = Header(None), _=Depends(login_required), _csrf=Depends(csrf_check)):
    business_id = get_current_business_id(request)
    if request.headers.get("content-type") == "application/json":
        body = await request.json()
    else:
        form = await request.form()
        body = {k: v for k, v in form.items()}
        if "price" in body: body["price"] = float(body["price"])
        if "stock" in body: body["stock"] = int(body["stock"])

    res = supabase.table("products").update(body).eq("id", product_id).eq("business_id", business_id).execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Product not found")
    
    product = res.data[0]
    stock_badge = f'<span class="badge badge-in-stock">In Stock ({product["stock"]})</span>' if product['stock'] > 0 else '<span class="badge badge-out">Out of Stock</span>'
    
    # If target is mobile card
    if hx_target and hx_target.startswith("m-product-"):
        return HTMLResponse(f"""
        <div class="m-card" id="m-product-{product['id']}">
            <div class="m-card-top">
                <span class="product-name" style="font-size:15px;">{product['name']}</span>
                {stock_badge}
            </div>
            <div>
                <span class="m-card-label">Price</span>
                <div class="amount" style="margin-top:2px;">₦{product['price']:.2f}</div>
            </div>
            <div class="m-card-actions">
                <button class="action-btn action-edit"
                    hx-get="/admin/products/{product['id']}/edit-form"
                    hx-target="#m-product-{product['id']}"
                    hx-swap="outerHTML">✎ Edit</button>
                <button class="action-btn action-delete"
                    hx-delete="/api/products/{product['id']}"
                    hx-confirm="Delete this product?"
                    hx-target="#m-product-{product['id']}"
                    hx-swap="outerHTML">Delete</button>
            </div>
        </div>
        """)

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


# ── Delete product (DELETE) ──────────────────────────────
@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: str, request: Request, _=Depends(login_required), _csrf=Depends(csrf_check)):
    business_id = get_current_business_id(request)
    resp = supabase.table("products") \
        .delete() \
        .eq("id", product_id) \
        .eq("business_id", business_id) \
        .execute()
    if resp.data:
        return Response(status_code=200)   # empty 200 tells HTMX to remove the element
    raise HTTPException(status_code=404, detail="Product not found")


@app.get("/api/orders")
async def api_get_orders(request: Request, status: str = None):
    business_id = get_current_business_id(request)
    return get_all_orders(business_id, status=status)

@app.get("/api/orders/pending-count")
async def pending_order_count(request: Request, _=Depends(login_required)):
    business_id = get_current_business_id(request)
    res = supabase.table("orders") \
        .select("id") \
        .eq("business_id", business_id) \
        .eq("status", "pending") \
        .execute()
    count = len(res.data or [])
    return str(count)


@app.patch("/api/orders/{order_id}/status")
async def api_update_order_status(order_id: int, request: Request, hx_target: str = Header(None), _=Depends(login_required), _csrf=Depends(csrf_check)):
    business_id = get_current_business_id(request)
    body = await request.json()
    new_status = body.get("status")
    if new_status not in ("pending", "confirmed", "cancelled"):
        raise HTTPException(status_code=400, detail="Invalid status")
    
    result = update_order_status(order_id, business_id, new_status)
    if not result:
        raise HTTPException(status_code=404, detail="Order not found")
        
    order = result[0]
    if isinstance(order.get("items"), str):
        order["items"] = json.loads(order["items"])
        
    items_html = "".join([f'<div>{i["name"]} <span style="color:var(--muted)">× {i["quantity"]}</span></div>' for i in order['items']])
    badge_class = f"badge-{order['status']}"
    
    # If target is mobile card
    if hx_target and hx_target.startswith("m-order-"):
        return HTMLResponse(f"""
        <div class="m-card" id="m-order-{order['id']}">
            <div class="m-card-top">
                <div class="order-id">#{order['id']}</div>
                <span class="badge {badge_class}">{order['status'].capitalize()}</span>
            </div>
            <div class="m-card-meta">
                <div class="m-card-field">
                    <span class="m-card-label">Customer</span>
                    <span class="m-card-val customer-name">{order['customer_name']}</span>
                </div>
                <div class="m-card-field">
                    <span class="m-card-label">Total</span>
                    <span class="m-card-val amount">₦{order['total']:.2f}</span>
                </div>
            </div>
            <div class="item-list">{items_html}</div>
            <div class="m-card-actions">
                <span style="color: #b5b4c0; font-size: 12px; font-style: italic;">Done</span>
            </div>
        </div>
        """)

    return HTMLResponse(f"""
    <tr id="order-{order['id']}">
        <td><span class="order-id">#{order['id']}</span></td>
        <td><span class="customer-name">{order['customer_name']}</span></td>
        <td><div class="item-list">{items_html}</div></td>
        <td><span class="amount">₦{order['total']:.2f}</span></td>
        <td><span class="badge {badge_class}">{order['status'].capitalize()}</span></td>
        <td>
            <div class="actions-cell">
                <span style="color: #b5b4c0; font-size: 12px; font-style: italic;">Done</span>
            </div>
        </td>
    </tr>
    """)


@app.get("/api/stats")
async def api_stats(request: Request):
    business_id = get_current_business_id(request)
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
async def admin_dashboard(request: Request, _=Depends(login_required)):
    user = await get_current_user(request)
    business_id = get_current_business_id(request)
    # Fetch all businesses for the switcher dropdown
    biz_res = supabase.table("businesses").select("id", "name").order("id").execute()
    businesses = biz_res.data or []

    template = templates.get_template("admin.html")
    content = template.render({
        "request": request,
        "businesses": businesses,
        "current_business_id": business_id,
        "is_superadmin": user.get("is_superadmin", False)
    })
    return HTMLResponse(content)


@app.get("/admin/products", response_class=HTMLResponse)
async def admin_products_partial(request: Request, _=Depends(login_required)):
    """Return only the product table (HTMX partial)."""
    business_id = get_current_business_id(request)  # will be replaced by real auth later
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
async def product_edit_form(product_id: str, hx_target: str = Header(None), _=Depends(login_required)):
    res = supabase.table("products").select("*").eq("id", product_id).single().execute()
    product = res.data
    if not product:
        return HTMLResponse("Product not found", status_code=404)
    
    # If target is mobile card
    if hx_target and hx_target.startswith("m-product-"):
        return HTMLResponse(f"""
        <div class="m-card" id="m-product-{product['id']}" style="background:var(--surface2); border:1.5px solid var(--accent2);">
            <div style="display:flex; flex-direction:column; gap:12px;">
                <div class="m-card-field">
                    <label class="m-card-label">Name</label>
                    <input type="text" name="name" value="{product['name']}" 
                           style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; width:100%; font-size:14px;">
                </div>
                <div style="display:flex; gap:12px;">
                    <div class="m-card-field" style="flex:1;">
                        <label class="m-card-label">Price (₦)</label>
                        <input type="number" name="price" step="0.01" value="{product['price']}" 
                               style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; width:100%; font-size:14px;">
                    </div>
                    <div class="m-card-field" style="flex:1;">
                        <label class="m-card-label">Stock</label>
                        <input type="number" name="stock" value="{product['stock']}" 
                               style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:8px 12px; border-radius:8px; width:100%; font-size:14px;">
                    </div>
                </div>
                <div class="m-card-actions">
                    <button class="action-btn action-confirm" style="flex:1;"
                            hx-put="/api/products/{product['id']}" hx-include="closest .m-card"
                            hx-target="#m-product-{product['id']}" hx-swap="outerHTML">Save</button>
                    <button class="action-btn action-delete" style="flex:1;"
                            hx-get="/admin/products/{product['id']}/view"
                            hx-target="#m-product-{product['id']}" hx-swap="outerHTML">Cancel</button>
                </div>
            </div>
        </div>
        """)

    return HTMLResponse(f"""
    <tr id="product-{product['id']}" style="background:var(--surface2);">
        <td><input type="text" name="name" value="{product['name']}" 
            style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:6px 10px; border-radius:8px; width:100%; font-size:13px;"></td>
        <td><input type="number" name="price" step="0.01" value="{product['price']}" 
            style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:6px 10px; border-radius:8px; width:100px; font-size:13px;"></td>
        <td><input type="number" name="stock" value="{product['stock']}" 
            style="background:var(--surface); border:1px solid var(--border); color:var(--text); padding:6px 10px; border-radius:8px; width:70px; font-size:13px;"></td>
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
async def product_view_row(product_id: str, hx_target: str = Header(None), _=Depends(login_required)):
    res = supabase.table("products").select("*").eq("id", product_id).single().execute()
    product = res.data
    if not product:
        return HTMLResponse("", status_code=404)
    
    stock_badge = f'<span class="badge badge-in-stock">In Stock ({product["stock"]})</span>' if product['stock'] > 0 else '<span class="badge badge-out">Out of Stock</span>'
    
    # If target is mobile card
    if hx_target and hx_target.startswith("m-product-"):
        return HTMLResponse(f"""
        <div class="m-card" id="m-product-{product['id']}">
            <div class="m-card-top">
                <span class="product-name" style="font-size:15px;">{product['name']}</span>
                {stock_badge}
            </div>
            <div>
                <span class="m-card-label">Price</span>
                <div class="amount" style="margin-top:2px;">₦{product['price']:.2f}</div>
            </div>
            <div class="m-card-actions">
                <button class="action-btn action-edit"
                    hx-get="/admin/products/{product['id']}/edit-form"
                    hx-target="#m-product-{product['id']}"
                    hx-swap="outerHTML">✎ Edit</button>
                <button class="action-btn action-delete"
                    hx-delete="/api/products/{product['id']}"
                    hx-confirm="Delete this product?"
                    hx-target="#m-product-{product['id']}"
                    hx-swap="outerHTML">Delete</button>
            </div>
        </div>
        """)

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
async def admin_orders_partial(request: Request, status: str = None, _=Depends(login_required)):
    business_id = get_current_business_id(request)
    query = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("created_at", desc=True)
    if status:
        query = query.eq("status", status)

    orders_response = query.execute()
    raw_orders = orders_response.data or []

    # Parse JSON string items if necessary
    for order in raw_orders:
        if isinstance(order.get("items"), str):
            order["items"] = json.loads(order["items"])

    # Manual rendering to bypass the TemplateResponse bug
    template = templates.get_template("_orders.html")
    content = template.render({"request": request, "orders": raw_orders})
    return HTMLResponse(content)


# Override the old /api/products GET to return JSON (unchanged) –
# but for HTMX we'll use the /admin/products route above.
# The existing /api/products can remain as the pure JSON API.


@app.get("/admin/products/add-form", response_class=HTMLResponse)
async def product_add_form(_=Depends(login_required)):
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
async def debug_orders_raw(request: Request):
    business_id = get_current_business_id(request)
    res = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .order("created_at", desc=True) \
        .execute()
    return {"count": len(res.data or []), "data": res.data}


# ── Webhook & Channel Management ─────────────────────────

@app.post("/webhook/{token}")
async def telegram_webhook(token: str, request: Request):
    """Receive updates for a specific bot token."""
    # Verify the token belongs to an active channel
    channel = supabase.table("channels") \
        .select("business_id") \
        .eq("channel_type", "telegram") \
        .eq("identifier", token) \
        .eq("is_active", True) \
        .single() \
        .execute()
    if not channel.data:
        raise HTTPException(status_code=404, detail="Channel not found")

    business_id = channel.data["business_id"]

    # Create a dedicated bot instance for this token
    # We use a custom request object to match the app's config if needed
    bot = telegram.Bot(token)
    async with bot:
        body = await request.json()
        update = Update.de_json(body, bot)
        if update:
            # Set the business_id for the context of this specific update
            # Note: We temporarily store it in the app's bot_data for the handlers
            # to pick up, though in a highly concurrent environment this should 
            # ideally be handled via a more robust context-local storage.
            telegram_app.bot_data["business_id"] = business_id
            await telegram_app.process_update(update)
    return {"ok": True}


@app.post("/admin/channels/add")
async def add_channel(request: Request, token: str = Form(...), business_name: str = Form("New Business"), _=Depends(login_required), _csrf=Depends(csrf_check)):
    """Add a new Telegram channel and create a business if needed."""
    # Create a new business
    slug = business_name.lower().replace(" ", "-")
    business = supabase.table("businesses").insert({
        "name": business_name,
        "slug": slug,
    }).execute()
    business_id = business.data[0]["id"]

    # Insert the channel
    supabase.table("channels").insert({
        "business_id": business_id,
        "channel_type": "telegram",
        "identifier": token,
        "is_active": True,
    }).execute()

    # Register the webhook
    base_url = os.getenv("RAILWAY_PUBLIC_URL", "https://your-app.up.railway.app")
    try:
        bot = telegram.Bot(token)
        await bot.set_webhook(f"{base_url}/webhook/{token}")
        message = f"✅ Channel connected! New business ID: {business_id}"
    except Exception as e:
        message = f"⚠️ Business created, but webhook failed: {e}"

    return HTMLResponse(f"<p class='text-green-400'>{message}</p>")


@app.get("/admin/connect-form", response_class=HTMLResponse)
async def connect_form(request: Request, _=Depends(login_required)):
    template = templates.get_template("_connect.html")
    return HTMLResponse(template.render({"request": request}))


# ── Settings Page ───────────────────────────────────────
@app.post("/admin/switch-business/{business_id}")
async def switch_business(request: Request, business_id: int, _=Depends(login_required), _csrf=Depends(csrf_check)):
    request.session["current_business_id"] = business_id
    return RedirectResponse("/admin", status_code=303)

@app.get("/api/businesses")
async def get_businesses(_=Depends(login_required)):
    """Return all businesses (for the admin switcher dropdown)."""
    res = supabase.table("businesses").select("id", "name").order("id").execute()
    return res.data or []

@app.get("/admin/settings", response_class=HTMLResponse)
async def settings_page(request: Request, _=Depends(login_required)):
    business_id = get_current_business_id(request)  # will be dynamic after multi‑admin auth
    res = supabase.table("businesses") \
        .select("settings") \
        .eq("id", business_id) \
        .single() \
        .execute()
    settings = res.data.get("settings", {}) if res.data else {}
    template = templates.get_template("_settings.html")
    content = template.render({"request": request, "settings": settings})
    return HTMLResponse(content)

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard_home(request: Request, _=Depends(login_required)):
    business_id = get_current_business_id(request)

    # Stats (orders today, revenue this month, pending, in-stock)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    orders_res = supabase.table("orders") \
        .select("*") \
        .eq("business_id", business_id) \
        .execute()
    all_orders = orders_res.data or []

    products_res = supabase.table("products") \
        .select("*") \
        .eq("business_id", business_id) \
        .execute()
    all_products = products_res.data or []

    today_orders = [o for o in all_orders if o["created_at"][:10] == today]
    month_orders = [o for o in all_orders if o["created_at"][:7] == this_month]
    pending = [o for o in all_orders if o["status"] == "pending"]
    in_stock = len([p for p in all_products if p["stock"] > 0])

    month_revenue = sum(o["total"] for o in month_orders if o["status"] == "confirmed")

    stats = {
        "today_orders": len(today_orders),
        "month_revenue": month_revenue,
        "pending_orders": len(pending),
        "in_stock": in_stock,
    }

    # Top 5 products by quantity sold
    product_sales = {}
    for order in all_orders:
        if order["status"] == "confirmed":
            items = order.get("items", [])
            if isinstance(items, str):
                items = json.loads(items)
            for item in items:
                name = item.get("name", "Unknown")
                qty = item.get("quantity", 0)
                product_sales[name] = product_sales.get(name, 0) + qty

    top_products = sorted(product_sales.items(), key=lambda x: x[1], reverse=True)[:5]
    top_products_list = [{"name": name, "total_sold": qty} for name, qty in top_products]

    template = templates.get_template("_dashboard.html")
    content = template.render({
        "request": request,
        "stats": stats,
        "top_products": top_products_list
    })
    return HTMLResponse(content)

@app.post("/api/business/settings")
async def save_settings(request: Request, _=Depends(login_required), _csrf=Depends(csrf_check)):
    business_id = get_current_business_id(request)
    form = await request.form()
    settings = {
        "tone": form.get("tone", "friendly"),
        "sales_style": form.get("sales_style", "balanced"),
        "collect_phone": form.get("collect_phone") == "on",
        "collect_email": form.get("collect_email") == "on",
        "mention_price_only_when_asked": form.get("mention_price_only_when_asked") == "on",
        "admin_telegram_ids": form.get("admin_telegram_ids", ""),
        "admin_whatsapp_numbers": form.get("admin_whatsapp_numbers", ""),
    }
    supabase.table("businesses") \
        .update({"settings": settings}) \
        .eq("id", business_id) \
        .execute()
    return HTMLResponse("<p class='text-green-400'>✅ Settings saved!</p>")


# ── Entry Point ───────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))   # Railway sets PORT=8080
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)