# STRUCTURE.md – Sell! Codebase Guide

This document is for anyone who needs to understand, modify, or extend the Sell! platform.  
It assumes you’ve already read the `README.md`.

---

## 📁 File Map
```text
Cupabooks-main/
├── main.py # FastAPI application (all routes, lifespan, Telegram bot setup)
├── bot.py # AI conversation logic, order parsing, notification sending
├── admin.py # Telegram admin commands (inventory, orders)
├── orders.py # Order CRUD functions (Supabase)
├── catalog.py # Product CRUD functions (Supabase)
├── auth.py # Password verification & login dependency
├── supabase_client.py # Supabase client singleton
├── templates/ # Jinja2 partials for the admin dashboard
│ ├── admin.html # Full page layout (sidebar, topbar, main area)
│ ├── login.html # Standalone login form
│ ├── _products.html # Product table + inline add/edit forms
│ └── _orders.html # Order table + status filters + actions
├── static/ # Static assets (CSS, images)
├── requirements.txt # Python dependencies
└── README.md # Project overview & quick start
```

---

## 🧱 Module Responsibilities

### `main.py`
- **FastAPI app creation** with lifespan (`startup`/`shutdown`).
- **Telegram bot integration** via `python-telegram-bot`: builds the `Application`, registers all handlers, starts polling.
- **Session middleware** (`starlette.middleware.sessions.SessionMiddleware`).
- **All HTTP routes**:
  - `/admin` and partials (`/admin/products`, `/admin/orders`, …) – protected by `login_required`.
  - `/api/products` (CRUD) – protected by `login_required`.
  - `/api/orders` (read + status update) – protected by `login_required`.
  - `/api/stats` (read‑only, for future analytics).
  - `/login` (GET/POST), `/logout`.
- **Logging** and **error handling** for the Telegram bot.

### `bot.py`
- **AI logic:** builds context from the catalog, calls Groq, parses the response.
- **Order flow:** parses the `##ORDER##` signal, calls `save_order`.
- **`save_order`**: validates items (existence + stock), enriches them, calls `orders.create_order`, and sends the admin notification with inline buttons.
- **`validate_order_items`**: returns `(valid_items, invalid_items)`.
- **Cart** and **conversation memory** (in‑memory, per user).
- **Admin AI logic** for when a known admin chats with the bot.

### `admin.py`
- **Admin Telegram commands**: `/addproduct`, `/outofstock`, `/restock`, `/setstock`, `/deleteproduct`, `/pending`, `/confirm`, `/cancelorder`, `/products` (all scoped by `business_id`).
- **`admin_only` decorator** for permission checks.

### `orders.py`
- **CRUD**: `create_order`, `get_orders_by_user`, `get_all_orders`, `update_order_status`.
- **`format_order_summary`** for Telegram display.
- All functions accept a `business_id` parameter.

### `catalog.py`
- **CRUD**: `get_all_products`, `search_products`, `get_product_by_id`, `get_product_by_name`.
- **Formatting helpers**: `format_product`, `format_catalog`.

### `auth.py`
- **Password verification** using `hashlib.sha256` (compare hashed password).
- **`login_required`** FastAPI dependency that returns `401` if the session is not authenticated.

### `supabase_client.py`
- Creates the Supabase client using the **service‑role key** from the environment.

---

## 🔁 Data Flow

1. **Customer message** → `main.py` → `message_handler` → `bot.handle_message`.
2. `handle_message` → loads catalog, builds AI prompt → Groq API → reply.
3. If the reply contains `##ORDER##`, `parse_order_signal` extracts customer name + items + location.
4. `save_order` is called:
   - `validate_order_items` checks every product ID against the catalog.
   - Valid items are enriched with name/price.
   - `orders.create_order` inserts a pending order into Supabase.
   - Admin is notified with inline `Confirm`/`Cancel` buttons.
5. When admin taps a button, `main.handle_order_callback` updates the order status and notifies the customer.

---

## 🧩 Key Patterns

### Manual template rendering
Because of a Starlette/Jinja2 bug on Python 3.13, all routes that return HTML use manual rendering:
```python
template = templates.get_template("file.html")
content = template.render({"request": request, ...})
return HTMLResponse(content)
```
Always follow this pattern when adding new admin pages.

### UUID product IDs
Product IDs are UUIDs (Supabase default). All route parameters that accept a product ID must be typed as `str`, not `int`.

### Business isolation
Every database query includes `.eq("business_id", business_id)`. The business_id is resolved at startup from the `channels` table and stored in `telegram_app.bot_data`.

### Inline admin notifications
The admin notification uses `InlineKeyboardMarkup` with callback data like `confirm:<order_id>`. The callback handler in `main.py` processes these actions.

---

## 🛡️ Security Notes
- The admin password is hashed (SHA‑256) and compared against `ADMIN_PASSWORD` env var.
- The service‑role key must never be exposed to the frontend or committed to the repository.
- The Telegram bot token is masked in logs using a custom log filter (if implemented, or planned).
- All state‑changing API routes require authentication.

---

## 🧪 Testing (Manual Checklist)
After major changes, verify:
1. **Telegram bot**: `/start`, `/catalog`, placing an order, receiving admin notification.
2. **Admin dashboard**: login, product CRUD (add/edit/delete), order management (filter, confirm, cancel).
3. **Multi‑tenant**: orders created for business ID 1 only appear in that business’s admin.
