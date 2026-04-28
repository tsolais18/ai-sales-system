from supabase_client import supabase


def get_all_products():
    response = supabase.table("products").select("*").gt("stock", 0).execute()
    return response.data or []


def search_products(query: str):
    response = (
        supabase.table("products")
        .select("*")
        .or_(f"name.ilike.%{query}%,description.ilike.%{query}%")
        .gt("stock", 0)
        .execute()
    )
    return response.data or []


def get_product_by_id(product_id: int):
    response = supabase.table("products").select("*").eq("id", product_id).single().execute()
    return response.data


def format_product(product: dict) -> str:
    in_stock = product.get("stock", 0) > 0
    return (
        f"📦 *{product['name']}*\n"
        f"📝 {product['description']}\n"
        f"💰 ₦{product['price']:,}\n"
        f"{'✅ In Stock' if in_stock else '❌ Out of Stock'}\n"
        f"🆔 ID: `{product['id']}`"
    )


def format_catalog(products: list) -> str:
    if not products:
        return "No products found."
    return "\n\n".join([format_product(p) for p in products])