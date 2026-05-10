# Sell! – AI Sales Platform for Any Product Business

**One deployment, unlimited AI‑powered storefronts.**

Sell! is a multi‑tenant AI sales platform that lets businesses deploy a conversational AI agent inside Telegram. Customers chat naturally, the AI recommends products from the real catalog, and the system creates orders, reduces stock, and notifies the owner — all automatically.

Built for **any product‑based business**, not just bookstores.

---

## 🧠 What it does

- **AI Salesperson**: Groq‑powered Llama 3.3 converses with customers, answers product questions, and guides them through a full order flow (name → address → confirmation).
- **Multi‑tenant isolation**: Every business gets its own catalog, orders, and customer data — completely separate, running on a single deployment.
- **Admin dashboard**: Business owners manage products and orders through a clean, responsive web UI (HTMX + Tailwind CSS).
- **Telegram bot**: Customers interact through Telegram; the bot resolves which business they belong to and loads the correct catalog.
- **Order notifications**: Owners receive real‑time Telegram messages with inline **Confirm / Cancel** buttons.
- **Security**: Password‑protected admin dashboard, session middleware, rate limiting ready, AI input sanitization, and service‑role key isolation.

---

## 🧱 Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.13 + FastAPI + Uvicorn |
| AI Engine | Groq (Llama 3.3) |
| Database | Supabase (PostgreSQL) with Row‑Level Security |
| Bot Framework | python‑telegram‑bot |
| Frontend | Jinja2 + HTMX + Tailwind CSS (no build step) |
| Hosting | Railway |
| Auth | SessionMiddleware + hashed password |

---

## 📁 Project Structure
```text
ai-sales-system-main/
├── main.py # FastAPI app, Telegram handlers, dashboard routes
├── bot.py # AI logic, conversation handling, order validation, save_order
├── admin.py # Admin Telegram commands (/addproduct, /pending, etc.)
├── orders.py # Order CRUD operations
├── catalog.py # Product CRUD operations
├── auth.py # Password hashing & login dependency
├── supabase_client.py # Supabase client setup
├── templates/
│ ├── admin.html # Full dashboard layout (sidebar + content area)
│ ├── login.html # Authentication page
│ ├── _products.html # Product table partial (add/edit/delete)
│ └── _orders.html # Order table partial (with status filters)
├── static/ # Static assets (CSS, images)
└── requirements.txt # Python dependencies
```

---

## 🗃️ Database Schema (simplified)

- **businesses** – `id`, `name`, `slug`, `settings (JSONB)`
- **channels** – `id`, `business_id`, `channel_type`, `identifier` (bot token)
- **products** – `id (UUID)`, `business_id`, `name`, `description`, `price`, `stock`
- **orders** – `id (UUID)`, `business_id`, `customer_name`, `telegram_id`, `items (JSONB)`, `total`, `status`, `location`

All tables scoped by `business_id` for tenant isolation.

---

## 🔁 Core Flow
```text
Customer message (Telegram)
↓
Channel resolver → identify business_id
↓
Load catalog & AI context
↓
Groq AI → response + optional ##ORDER## signal
↓
Validate order items (product exists, stock > 0)
↓
Create order → reduce stock → notify admin with Confirm/Cancel buttons
↓
Admin confirms/cancels from Telegram or web dashboard
```

---

## 🔒 Security Features

- Dashboard and API routes protected by password authentication
- Service‑role key never exposed to the frontend
- AI input sanitized to block prompt injection
- Row‑Level Security enabled (Supabase)
- Rate limiter ready to activate on AI endpoint
- Telegram bot token masked in logs

---

## 📈 Roadmap

- **AI Control Panel** – business owners customize AI tone & behavior
- **Analytics Dashboard** – revenue, top products, conversion rates
- **WhatsApp Channel** – same engine, new messaging frontend
- **Webhook Mode** – more reliable Telegram connection
- **Multi‑admin Support** – role‑based access per business
- **Public Landing Page** – marketing site for Sell!

---

## 🚀 Getting Started (Local Dev)

1. Clone the repo
2. Create a `.env` file with required keys:
```text
TELEGRAM_BOT_TOKEN=...
GROQ_API_KEY=...
SUPABASE_URL=...
SUPABASE_KEY=...
ADMIN_PASSWORD=...
SECRET_KEY=...
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Run the app:
```bash
uvicorn main:app --reload --port 8000
```
Visit http://localhost:8000/admin and log in with your admin password.

🧑💻 Built By
A solo builder turning a bookstore’s ordering problem into a platform for any product business.

Sell! is still young, but it's architected for scale.
