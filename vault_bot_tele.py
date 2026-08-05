import os
import aiosqlite
import hashlib
import random
import time
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
)

# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("TELEGRAM_VAULT_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_VAULT_BOT_TOKEN belum diset di Variables Railway!")

# Path Absolut agar membaca file fisik database yang sama persis
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "cosa_nostra.db")
WIB = timezone(timedelta(hours=7))

# ==========================================
# HELPER KONEKSI DATABASE (WAL MODE & AUTO TABLE CREATION)
# ==========================================
@asynccontextmanager
async def get_db_connection():
    """
    Membuka koneksi SQLite dengan konfigurasi WAL mode.
    """
    db = await aiosqlite.connect(DB_NAME, timeout=30.0)
    try:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        yield db
    finally:
        await db.close()

async def init_db():
    """Membuat semua tabel yang dibutuhkan jika belum ada."""
    async with get_db_connection() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                koin INTEGER DEFAULT 10000,
                bank_balance INTEGER DEFAULT 0,
                bank_loan INTEGER DEFAULT 0,
                vitality INTEGER DEFAULT 100,
                gelar_tier TEXT DEFAULT 'G0',
                heat INTEGER DEFAULT 0,
                respect INTEGER DEFAULT 0,
                admin_tier INTEGER DEFAULT 0,
                jailed_until INTEGER DEFAULT 0,
                bounty INTEGER DEFAULT 0,
                crew_id INTEGER DEFAULT 0,
                last_work INTEGER DEFAULT 0,
                last_daily INTEGER DEFAULT 0,
                job_active TEXT,
                job_finish_time INTEGER DEFAULT 0,
                last_business_collect INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_code TEXT,
                item_type TEXT,
                durability INTEGER DEFAULT 100,
                cert_number TEXT UNIQUE,
                is_equipped INTEGER DEFAULT 0,
                acquired_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS certificates (
                cert_number TEXT PRIMARY KEY,
                user_id INTEGER,
                asset_code TEXT,
                asset_name TEXT,
                purchase_price INTEGER,
                issue_date TEXT,
                sha256_hash TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                action TEXT,
                target_id INTEGER,
                details TEXT,
                timestamp INTEGER
            )
        """)
        await db.commit()

async def post_init(application):
    await init_db()

# ==========================================
# DATABASE HELPER & USER MANAGEMENT
# ==========================================
USER_COLUMNS = "user_id, username, koin, bank_balance, bank_loan, vitality, gelar_tier, heat, respect, admin_tier, jailed_until, bounty, crew_id, last_work, last_daily, job_active, job_finish_time, last_business_collect"

async def get_or_create_user(db, user_id: int, username: str):
    """Fungsi standar untuk mengambil/membuat data user & memastikan struktur DB siap."""
    # Safety Check: Pastikan tabel terbuat jika belum
    await init_db()

    async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
        user = await cursor.fetchone()
        
    if user:
        if user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()
            async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
                user = await cursor.fetchone()
        return user
    else:
        await db.execute(
            """INSERT INTO users (user_id, username, koin, bank_balance, bank_loan, vitality, gelar_tier, heat, respect, admin_tier, jailed_until, bounty, crew_id) 
               VALUES (?, ?, 10000, 0, 0, 100, 'G0', 0, 0, 0, 0, 0, 0)""",
            (user_id, username)
        )
        await db.commit()
        
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

# ==========================================
# KATALOG ITEM & BLACKMARKET
# ==========================================
CATALOG = {
    # MAKANAN (F1-F10)
    "F1": {"name": "Panini Salami", "type": "food", "price": 500, "vit": 20, "desc": "Sandwich klasik Italia"},
    "F2": {"name": "Pasta Carbonara", "type": "food", "price": 800, "vit": 25, "desc": "Pasta creamy dengan bacon & keju"},
    "F3": {"name": "Risotto Truffle", "type": "food", "price": 2000, "vit": 40, "desc": "Risotto premium dengan truffle asli"},
    "F4": {"name": "Steak Wagyu Premium", "type": "food", "price": 3500, "vit": 50, "desc": "Daging wagyu grade A"},
    "F5": {"name": "Pizza Napoli", "type": "food", "price": 600, "vit": 22, "desc": "Classic pizza Napoli"},
    "F6": {"name": "Ossobuco Milanese", "type": "food", "price": 2500, "vit": 45, "desc": "Daging sapi rebung dengan saus tomat"},
    "F7": {"name": "Lasagna Bolognese", "type": "food", "price": 1200, "vit": 30, "desc": "Lasagna tradisional Bolognese"},
    "F8": {"name": "Tiramisu Italiano", "type": "food", "price": 1500, "vit": 35, "desc": "Dessert manis dengan kopi & mascarpone"},
    "F9": {"name": "Caviar Deluxe", "type": "food", "price": 5000, "vit": 60, "desc": "Caviar murni dari Rusia"},
    "F10": {"name": "Foie Gras", "type": "food", "price": 4500, "vit": 55, "desc": "Hati angsa premium"},

    # MINUMAN (D1-D10)
    "D1": {"name": "Espresso Italian", "type": "drink", "price": 300, "vit": 15, "desc": "Kopi espresso murni"},
    "D2": {"name": "Cappuccino Premium", "type": "drink", "price": 600, "vit": 20, "desc": "Cappuccino foam lembut"},
    "D3": {"name": "Whiskey Scotch 18Y", "type": "drink", "price": 2000, "vit": 35, "desc": "Whiskey aged 18 tahun"},
    "D4": {"name": "Vodka Beluga", "type": "drink", "price": 3000, "vit": 40, "desc": "Vodka premium Rusia"},
    "D5": {"name": "Wine Bordeaux 1995", "type": "drink", "price": 2500, "vit": 38, "desc": "Wine Bordeaux vintage 1995"},
    "D6": {"name": "Champagne Dom Perignon", "type": "drink", "price": 4000, "vit": 45, "desc": "Champagne mewah"},
    "D7": {"name": "Rum Bacardi Legacy", "type": "drink", "price": 1500, "vit": 28, "desc": "Rum klasik Karibia"},
    "D8": {"name": "Gin Bombay", "type": "drink", "price": 1000, "vit": 22, "desc": "Gin premium botanicals"},
    "D9": {"name": "Martini Dry", "type": "drink", "price": 2200, "vit": 32, "desc": "Cocktail klasik gentleman"},
    "D10": {"name": "Sake Premium Dassai", "type": "drink", "price": 3500, "vit": 42, "desc": "Sake premium Jepang"},

    # SENJATA (W1-W12)
    "W1": {"name": "Pistol 9MM", "type": "weapon", "price": 5000, "desc": "Entry-level senjata"},
    "W2": {"name": "Revolver .45", "type": "weapon", "price": 8000, "desc": "Powerful handgun"},
    "W3": {"name": "Shotgun Combat", "type": "weapon", "price": 15000, "desc": "Heavy damage"},
    "W4": {"name": "Rifle Sniper", "type": "weapon", "price": 20000, "desc": "Precision weapon"},
    "W5": {"name": "SMG MP5", "type": "weapon", "price": 12000, "desc": "High fire rate"},
    "W6": {"name": "Knife Combat", "type": "weapon", "price": 2000, "desc": "Silent stealth weapon"},
    "W7": {"name": "Grenade", "type": "weapon", "price": 5500, "desc": "Area explosive"},
    "W8": {"name": "C4 Explosive", "type": "weapon", "price": 18000, "desc": "Bank heist essential"},
    "W9": {"name": "Silenced Pistol", "type": "weapon", "price": 10000, "desc": "Silent assassin tool"},
    "W10": {"name": "Desert Eagle Gold", "type": "weapon", "price": 25000, "desc": "Prestige weapon emas"},
    "W11": {"name": "Combat Shotgun Tactical", "type": "weapon", "price": 22000, "desc": "Balanced heavy weapon"},
    "W12": {"name": "Plasma Rifle (Rare)", "type": "weapon", "price": 50000, "desc": "Senjata legendaris futuristik"},

    # ARMOR (A1-A11)
    "A1": {"name": "Leather Jacket", "type": "armor", "price": 3000, "desc": "Jaket kulit mafia"},
    "A2": {"name": "Kevlar Vest", "type": "armor", "price": 10000, "desc": "Tactical kevlar"},
    "A3": {"name": "Police Riot Gear", "type": "armor", "price": 12000, "desc": "Gear bekas polisi"},
    "A4": {"name": "Tactical Plate Carrier", "type": "armor", "price": 18000, "desc": "Professional tactical gear"},
    "A5": {"name": "Chainmail Undersuit", "type": "armor", "price": 8000, "desc": "Proteksi blade weapon"},
    "A6": {"name": "Combat Helmet", "type": "armor", "price": 7000, "desc": "Proteksi khusus kepala"},
    "A7": {"name": "Full Tactical Suit", "type": "armor", "price": 25000, "desc": "Complete tactical suit"},
    "A8": {"name": "Bomb Defusal Suit", "type": "armor", "price": 22000, "desc": "Pelindung khusus ledakan"},
    "A9": {"name": "Neon Energy Shield", "type": "armor", "price": 35000, "desc": "Tech defense shield"},
    "A10": {"name": "Cyberpunk Exo-Skeleton", "type": "armor", "price": 60000, "desc": "Exo-skeleton penguat"},
    "A11": {"name": "Royal Guard Armor", "type": "armor", "price": 80000, "desc": "Armor legendaris"},

    # PERHIASAN (J1-J15)
    "J1": {"name": "Silver Ring", "type": "jewelry", "price": 2000, "desc": "Cincin perak murni"},
    "J2": {"name": "Gold Chain", "type": "jewelry", "price": 5000, "desc": "Rantai emas murni"},
    "J3": {"name": "Diamond Earring", "type": "jewelry", "price": 12000, "desc": "Anting berlian"},
    "J4": {"name": "Ruby Necklace", "type": "jewelry", "price": 25000, "desc": "Kalung ruby merah"},
    "J5": {"name": "Emerald Watch", "type": "jewelry", "price": 40000, "desc": "Jam tangan zamrud"},
    "J6": {"name": "Sapphire Crown", "type": "jewelry", "price": 60000, "desc": "Mahkota permata safir"},
    "J7": {"name": "Platinum Bracelet", "type": "jewelry", "price": 35000, "desc": "Gelang platina langka"},
    "J8": {"name": "Pearl Pendant", "type": "jewelry", "price": 15000, "desc": "Liontin mutiara"},
    "J9": {"name": "Onyx Cufflinks", "type": "jewelry", "price": 8000, "desc": "Kancing kemeja onyx"},
    "J10": {"name": "Diamond Ring Rare", "type": "jewelry", "price": 150000, "desc": "Cincin berlian langka"},
    "J11": {"name": "Gold Rolex Vintage", "type": "jewelry", "price": 90000, "desc": "Rolex vintage antik"},
    "J12": {"name": "Cartier Panther Ring", "type": "jewelry", "price": 200000, "desc": "Cincin panther kolektor"},
    "J13": {"name": "Fabergé Egg Replica", "type": "jewelry", "price": 300000, "desc": "Telur Fabergé bersejarah"},
    "J14": {"name": "Imperial Jade Pendant", "type": "jewelry", "price": 400000, "desc": "Liontin giok kekaisaran"},
    "J15": {"name": "Papal Ring Legendary", "type": "jewelry", "price": 500000, "desc": "Cincin bersejarah agung"},

    # PROPERTI (H1-H12)
    "H1": {"name": "Apartment Downtown", "type": "property", "price": 50000, "passive": 500, "desc": "Apartemen pusat kota"},
    "H2": {"name": "Suburban House", "type": "property", "price": 100000, "passive": 1000, "desc": "Rumah pinggiran kota"},
    "H3": {"name": "Luxury Villa", "type": "property", "price": 250000, "passive": 2500, "desc": "Villa mewah kolam renang"},
    "H4": {"name": "Beachfront Condo", "type": "property", "price": 400000, "passive": 4500, "desc": "Kondo pinggir pantai"},
    "H5": {"name": "Townhouse Central", "type": "property", "price": 120000, "passive": 1500, "desc": "Townhouse strategis"},
    "H6": {"name": "Penthouse Sky", "type": "property", "price": 600000, "passive": 7000, "desc": "Penthouse pencakar langit"},
    "H7": {"name": "Private Island Base", "type": "property", "price": 1500000, "passive": 15000, "desc": "Pulau pribadi terisolasi"},
    "H8": {"name": "Historic Castle", "type": "property", "price": 2000000, "passive": 18000, "desc": "Kastil megah bersejarah"},
    "H9": {"name": "Mountain Chalet", "type": "property", "price": 350000, "passive": 3800, "desc": "Chalet pegunungan"},
    "H10": {"name": "Country Estate", "type": "property", "price": 800000, "passive": 9000, "desc": "Perkebunan & tanah estate"},
    "H11": {"name": "Penthouse Malacca", "type": "property", "price": 1500000, "passive": 12000, "desc": "Penthouse Selat Malaka"},
    "H12": {"name": "Royal Palace", "type": "property", "price": 3000000, "passive": 20000, "desc": "Istana megah megah"},

    # KENDARAAN (V1-V14)
    "V1": {"name": "Scooter 125cc", "type": "vehicle", "price": 30000, "passive": 100, "desc": "Skuter hemat energi"},
    "V2": {"name": "Sedan Classic", "type": "vehicle", "price": 60000, "passive": 250, "desc": "Sedan hitam standar mafia"},
    "V3": {"name": "SUV Armored", "type": "vehicle", "price": 150000, "passive": 600, "desc": "SUV anti-peluru"},
    "V4": {"name": "Sports Car GT", "type": "vehicle", "price": 300000, "passive": 1200, "desc": "Mobil sport kencang"},
    "V5": {"name": "Luxury Sedan", "type": "vehicle", "price": 200000, "passive": 800, "desc": "Sedan mewah nyaman"},
    "V6": {"name": "Muscle Car V8", "type": "vehicle", "price": 100000, "passive": 400, "desc": "Mobil muscle gahar"},
    "V7": {"name": "Rolls-Royce Ghost", "type": "vehicle", "price": 500000, "passive": 3000, "desc": "Simbol kejayaan"},
    "V8": {"name": "Supercar Italian", "type": "vehicle", "price": 800000, "passive": 3500, "desc": "Supercar eksotis Italia"},
    "V9": {"name": "Helicopter Executive", "type": "vehicle", "price": 1000000, "passive": 4000, "desc": "Helikopter privat"},
    "V10": {"name": "Speedboat Offshore", "type": "vehicle", "price": 250000, "passive": 1000, "desc": "Kapal cepat penyelundup"},
    "V11": {"name": "Luxury Yacht", "type": "vehicle", "price": 1800000, "passive": 4800, "desc": "Yacht mewah lautan"},
    "V12": {"name": "Armored Truck", "type": "vehicle", "price": 400000, "passive": 1500, "desc": "Truk baja uang"},
    "V13": {"name": "Private Jet", "type": "vehicle", "price": 2500000, "passive": 6000, "desc": "Pesawat jet pribadi"},
    "V14": {"name": "Koenigsegg Hypercar", "type": "vehicle", "price": 2000000, "passive": 5000, "desc": "Hypercar tercepat"},

    # SERAGAM (S1-S7)
    "S1": {"name": "Casual Street", "type": "suit", "price": 10000, "desc": "Pakaian jalanan biasa"},
    "S2": {"name": "Business Formal", "type": "suit", "price": 25000, "desc": "Jas bisnis formal"},
    "S3": {"name": "Tactical Combat", "type": "suit", "price": 40000, "desc": "Seragam tempur taktis"},
    "S4": {"name": "Tailored Tuxedo", "type": "suit", "price": 60000, "desc": "Tuxedo kustom penjahit"},
    "S5": {"name": "Executive Suit", "type": "suit", "price": 80000, "desc": "Setelan pimpinan eksekutif"},
    "S6": {"name": "Silk Robe Mafia", "type": "suit", "price": 100000, "desc": "Jubah sutra elegan bos"},
    "S7": {"name": "Don Signature Suit", "type": "suit", "price": 150000, "desc": "Setelan khas Godfather"},

    # GELAR PANGKAT (G1-G7)
    "G1": {"name": "Made Man", "type": "gelar", "price": 5000, "desc": "Tier 1: Anggota resmi keluarga"},
    "G2": {"name": "Enforcer Primus", "type": "gelar", "price": 20000, "desc": "Tier 2: Eksekutor utama"},
    "G3": {"name": "Capo Regime", "type": "gelar", "price": 75000, "desc": "Tier 3: Kapten wilayah"},
    "G4": {"name": "Underboss Executive", "type": "gelar", "price": 250000, "desc": "Tier 4: Wakil pimpinan"},
    "G5": {"name": "Grand Consigliere", "type": "gelar", "price": 500000, "desc": "Tier 5: Penasihat agung"},
    "G6": {"name": "Caporegime Supremo", "type": "gelar", "price": 1000000, "desc": "Tier 6: Komandan tertinggi"},
    "G7": {"name": "Don / Donna Famiglia", "type": "gelar", "price": 2500000, "desc": "Tier 7: Penguasa puncak"},

    # BISNIS (B1-B13)
    "B1": {"name": "Kedai Kopi", "type": "business", "price": 10000, "passive": 300, "desc": "Kedai kopi pencuci uang"},
    "B2": {"name": "Toko Pakaian", "type": "business", "price": 30000, "passive": 800, "desc": "Boutique formal"},
    "B3": {"name": "Bar & Lounge", "type": "business", "price": 70000, "passive": 2000, "desc": "Tempat hiburan malam"},
    "B4": {"name": "Nightclub Underground", "type": "business", "price": 150000, "passive": 4500, "desc": "Klub malam ramai"},
    "B5": {"name": "Casino Malacca", "type": "business", "price": 250000, "passive": 6000, "desc": "Kasino perjudian besar"},
    "B6": {"name": "Kokain Lab Secret", "type": "business", "price": 200000, "passive": 5000, "desc": "Laboratorium tersembunyi"},
    "B7": {"name": "Laundromat Network", "type": "business", "price": 280000, "passive": 7000, "desc": "Pencucian uang otomatis"},
    "B8": {"name": "Pabrik Senjata Gelap", "type": "business", "price": 350000, "passive": 8000, "desc": "Manufaktur senjata ilegal"},
    "B9": {"name": "Import Export Cargo", "type": "business", "price": 450000, "passive": 9500, "desc": "Perusahaan kargo pelabuhan"},
    "B10": {"name": "Bank Swasta Lokal", "type": "business", "price": 500000, "passive": 10000, "desc": "Lembaga keuangan pribadi"},
    "B11": {"name": "Media Kontrol Pers", "type": "business", "price": 600000, "passive": 12000, "desc": "Perusahaan pers propaganda"},
    "B12": {"name": "Perusahaan Minyak", "type": "business", "price": 800000, "passive": 15000, "desc": "Kilang minyak bumi"},
    "B13": {"name": "Syndicate Kartel Global", "type": "business", "price": 1000000, "passive": 20000, "desc": "Konsorsium kriminal dunia"}
}

def generate_certificate(user_id: int, asset_code: str, asset_name: str, price: int) -> tuple:
    now = datetime.now(WIB)
    epoch = int(now.timestamp())
    date_str = now.strftime("%Y%m%d")
    date_formatted = now.strftime("%Y-%m-%d %H:%M:%S UTC+7")
    
    raw_hash = f"{user_id}:{asset_code}:{epoch}:{random.randint(1000, 9999)}"
    unique_hash = hashlib.sha256(raw_hash.encode()).hexdigest()[:8].upper()
    
    prefix_map = {"property": "PROP", "vehicle": "VEH", "weapon": "WPN", "armor": "ARM", "jewelry": "JWL", "suit": "SUIT", "business": "BIZ", "gelar": "GLR"}
    asset_type_prefix = prefix_map.get(CATALOG.get(asset_code, {}).get("type"), "GEN")
    
    cert_number = f"CSN-{asset_type_prefix}-{date_str}-{epoch}-{asset_code}-{unique_hash}"
    full_payload = f"{cert_number}|OWNER:{user_id}|ASSET:{asset_code}|PRICE:{price}|DATE:{date_formatted}"
    sha256_verification = hashlib.sha256(full_payload.encode()).hexdigest()
    
    return cert_number, sha256_verification, date_formatted

async def check_admin_tier(db, user_id: int) -> int:
    MY_PERMANENT_OWNER_ID = 8396793986  
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4  

    async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

# ==========================================
# COMMAND HANDLERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, username)

    text = (
        "💰 WELCOME TO COSA NOSTRA VAULT BOT\n\n"
        "Gunakan command berikut:\n"
        "🛍️ /shop - Katalog Toko\n"
        "💳 /beli [kode] - Beli barang dari Shop\n"
        "🏦 /bank - Kelola Rekening & Tabungan Bank\n"
        "🏠 /properties - Lihat Portofolio Properti & Aset\n"
        "💼 /business - Kelola Bisnis & Passive Income\n"
        "🎒 /portfolio - Cek Aset & Saldo Kekayaan\n"
        "📜 /certificate [id] - Verifikasi Sertifikat Aset\n\n"
        "🛠️ ADMINISTRATOR: /admin_panel"
    )
    await update.message.reply_text(text)

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    category = args[0].lower() if args else None

    categories = {
        "makanan": ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10"],
        "minuman": ["D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10"],
        "senjata": [f"W{i}" for i in range(1, 13)],
        "armor": [f"A{i}" for i in range(1, 12)],
        "perhiasan": [f"J{i}" for i in range(1, 16)],
        "properti": [f"H{i}" for i in range(1, 13)],
        "kendaraan": [f"V{i}" for i in range(1, 15)],
        "seragam": [f"S{i}" for i in range(1, 8)],
        "gelar": [f"G{i}" for i in range(1, 8)],
        "bisnis": [f"B{i}" for i in range(1, 14)]
    }

    if category in categories:
        codes = categories[category]
        text = f"🛍️ *KATALOG SHOP ({category.upper()})*\n\n"
        for code in codes:
            item = CATALOG[code]
            text += f"• *[{code}] {item['name']}* - {item['price']:,} Koin\n_{item['desc']}_\n\n"
        text += "Gunakan `/beli [kode]` untuk membeli."
    else:
        text = "🛍️ *COSA NOSTRA SHOP CATALOG*\n\nGunakan `/shop [kategori]` untuk memilih:\n\n"
        for cat in categories.keys():
            text += f"• `/shop {cat}`\n"
    
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_beli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Masukkan kode item. Contoh: `/beli F1` atau `/beli H1`", parse_mode="Markdown")
    
    code = context.args[0].upper()
    if code not in CATALOG:
        return await update.message.reply_text("❌ Kode item tidak ditemukan!")

    item = CATALOG[code]
    price = item["price"]
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, username)
        user_koin = user[2]
        user_vitality = user[5]
        current_gelar = user[6]

        if item["type"] == "gelar":
            async with db.execute(
                "SELECT id FROM inventory WHERE user_id = ? AND item_code = ?", 
                (user_id, code)
            ) as cursor:
                already_owned = await cursor.fetchone()

            if already_owned:
                return await update.message.reply_text(
                    f"❌ **PEMBELIAN DITOLAK:** Anda sudah pernah membeli gelar **{item['name']} ({code})**.",
                    parse_mode="Markdown"
                )

            current_tier_num = int(current_gelar.replace("G", "")) if current_gelar and current_gelar.startswith("G") else 0
            target_tier_num = int(code.replace("G", ""))

            if target_tier_num <= current_tier_num:
                return await update.message.reply_text(
                    f"❌ **PEMBELIAN DITOLAK:** Gelar Anda saat ini (**{current_gelar}**) sudah setara atau lebih tinggi dari **{code}**.",
                    parse_mode="Markdown"
                )

        if user_koin < price:
            return await update.message.reply_text(
                f"❌ Saldo tidak cukup! Harga: {price:,} Koin (Saldo: {user_koin:,} Koin)"
            )

        new_koin = user_koin - price

        if item["type"] in ["food", "drink"]:
            vit_gain = item.get("vit", 0)
            new_vit = min(100, user_vitality + vit_gain)
            await db.execute("UPDATE users SET koin = ?, vitality = ? WHERE user_id = ?", (new_koin, new_vit, user_id))
            await db.commit()
            return await update.message.reply_text(
                f"🍽️ *KONSUMSI BERHASIL*\n\n"
                f"Item: *[{code}] {item['name']}*\n"
                f"Harga: {price:,} Koin\n"
                f"Vitality Bertambah: +{vit_gain}% (Sisa Vitality: *{new_vit}%*)\n"
                f"Sisa Saldo: {new_koin:,} Koin",
                parse_mode="Markdown"
            )

        await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (new_koin, user_id))

        if item["type"] == "gelar":
            await db.execute("UPDATE users SET gelar_tier = ? WHERE user_id = ?", (code, user_id))

        cert_num, sha256_hash, issue_date = generate_certificate(user_id, code, item["name"], price)
        epoch_now = int(time.time())

        await db.execute(
            "INSERT INTO inventory (user_id, item_code, item_type, cert_number, acquired_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, code, item["type"], cert_num, epoch_now)
        )
        await db.execute(
            "INSERT INTO certificates VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cert_num, user_id, code, item["name"], price, issue_date, sha256_hash)
        )
        await db.commit()

    text = (
        f"✅ *TRANSAKSI BERHASIL*\n\n"
        f"Item: *[{code}] {item['name']}*\n"
        f"Harga: {price:,} Koin\n"
        f"Sisa Saldo: {new_koin:,} Koin\n"
        f"Certificate ID: `{cert_num}`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, username)
        koin, bank_balance, bank_loan = user[2], user[3], user[4]

        if not args or args[0].lower() == "balance":
            text = (
                f"🏦 *REKENING BANK COSA NOSTRA*\n\n"
                f"💵 Cash Tunai: *{koin:,} Koin*\n"
                f"📈 Tabungan Bank: *{bank_balance:,} Koin*\n"
                f"⚠️ Hutang Pinjaman: *{bank_loan:,} Koin*\n\n"
                f"Gunakan:\n"
                f"• `/bank deposit [jumlah]`\n"
                f"• `/bank withdraw [jumlah]`\n"
                f"• `/bank loan [jumlah]`\n"
                f"• `/bank payloan [jumlah]`"
            )
            return await update.message.reply_text(text, parse_mode="Markdown")

        action = args[0].lower()
        amount = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

        if action == "deposit":
            if amount <= 0 or amount > koin:
                return await update.message.reply_text("❌ Jumlah deposit tidak valid!")
            await db.execute("UPDATE users SET koin = koin - ?, bank_balance = bank_balance + ? WHERE user_id = ?", (amount, amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"✅ Berhasil menabung *{amount:,} Koin* ke Bank.", parse_mode="Markdown")

        elif action == "withdraw":
            if amount <= 0 or amount > bank_balance:
                return await update.message.reply_text("❌ Saldo bank tidak mencukupi!")
            await db.execute("UPDATE users SET koin = koin + ?, bank_balance = bank_balance - ? WHERE user_id = ?", (amount, amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 Berhasil menarik *{amount:,} Koin* dari Bank.", parse_mode="Markdown")

        elif action == "loan":
            if amount <= 0 or amount > 500000 or bank_loan > 0:
                return await update.message.reply_text("❌ Pinjaman tidak dapat diproses (Maks: 500K koin & tidak boleh ada pinjaman aktif)!")
            await db.execute("UPDATE users SET koin = koin + ?, bank_loan = ? WHERE user_id = ?", (amount, amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"⚠️ *PINJAMAN DISETUJUI:* Anda menerima *{amount:,} Koin*.", parse_mode="Markdown")

        elif action == "payloan":
            pay_amount = bank_loan if amount == 0 else min(amount, bank_loan)
            if pay_amount <= 0 or koin < pay_amount or bank_loan <= 0:
                return await update.message.reply_text("❌ Pembayaran pinjaman gagal!")
            await db.execute("UPDATE users SET koin = koin - ?, bank_loan = bank_loan - ? WHERE user_id = ?", (pay_amount, pay_amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"✅ Berhasil melunasi pinjaman sebesar *{pay_amount:,} Koin*.", parse_mode="Markdown")

async def cmd_business(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, username)
        last_collect = user[17] if len(user) > 17 and user[17] is not None else 0

        async with db.execute("SELECT item_code FROM inventory WHERE user_id = ? AND item_type IN ('business', 'property', 'vehicle')", (user_id,)) as cursor:
            assets = await cursor.fetchall()

        total_daily_passive = sum([CATALOG[code[0]].get("passive", 0) for code in assets if code[0] in CATALOG])

        if not args or args[0].lower() == "status":
            rem = max(0, 86400 - (now_epoch - last_collect))
            status_txt = "✅ *SIAP DIKLAIM!* Gunakan `/business collect`" if rem == 0 else f"⏳ Klaim lagi dalam: {rem//3600}j {(rem%3600)//60}m"
            text = (
                f"💼 *STATUS OPERASI BISNIS PASIF*\n\n"
                f"Unit Aset: {len(assets)} Unit\n"
                f"Hasil / 24 Jam: *+{total_daily_passive:,} Koin*\n"
                f"Status: {status_txt}"
            )
            return await update.message.reply_text(text, parse_mode="Markdown")

        if args[0].lower() == "collect":
            if total_daily_passive <= 0:
                return await update.message.reply_text("❌ Anda belum memiliki aset bisnis pasif.")
            if now_epoch - last_collect < 86400:
                rem = 86400 - (now_epoch - last_collect)
                return await update.message.reply_text(f"⏳ Klaim baru bisa dilakukan lagi dalam {rem//3600}j {(rem%3600)//60}m.")

            new_koin = user[2] + total_daily_passive
            await db.execute("UPDATE users SET koin = ?, last_business_collect = ? WHERE user_id = ?", (new_koin, now_epoch, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 *HASIL BISNIS:* Anda mendapatkan *+{total_daily_passive:,} Koin*.", parse_mode="Markdown")

async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        db_username = user[1]
        cash = user[2]
        bank = user[3]
        gelar = user[6]

        async with db.execute(
            "SELECT item_code, cert_number, acquired_at FROM inventory WHERE user_id = ? ORDER BY id DESC", 
            (user_id,)
        ) as cursor:
            inv = await cursor.fetchall()

        text = (
            f"🎒 *PORTOFOLIO KEKAYAAN & ASET*\n"
            f"User: *@*{db_username}\n"
            f"Cash Tunai: *{cash:,} Koin*\n"
            f"Saldo Bank: *{bank:,} Koin*\n"
            f"Gelar Utama: *{gelar}*\n"
            f"───────────────────\n"
        )

        if not inv:
            text += (
                "📦 *Koleksi Aset (0 Item):*\n"
                "ℹ️ *Anda belum memiliki aset atau properti di Vault.* \n\n"
                "💡 *Tips:* Gunakan perintah `/shop` untuk melihat katalog barang/aset, lalu beli menggunakan `/beli [kode_item]`."
            )
        else:
            text += f"📦 *Koleksi Aset ({len(inv)} Item):*\n"
            for code, cert, acquired_at in inv:
                item_info = CATALOG.get(code, {})
                name = item_info.get("name", code)
                
                date_str = datetime.fromtimestamp(acquired_at, tz=WIB).strftime("%Y-%m-%d") if acquired_at else "N/A"
                text += f"• *[{code}] {name}*\n  └ Cert: `{cert}` | _{date_str}_\n"

        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Masukkan ID Sertifikat. Contoh: `/certificate CSN-PROP-...`", parse_mode="Markdown")

    cert_id = context.args[0]
    async with get_db_connection() as db:
        async with db.execute("SELECT cert_number, user_id, asset_code, asset_name, purchase_price, issue_date, sha256_hash FROM certificates WHERE cert_number = ?", (cert_id,)) as cursor:
            cert = await cursor.fetchone()

        if not cert:
            return await update.message.reply_text("❌ *INVALID CERTIFICATE!*", parse_mode="Markdown")

        cert_num, owner_id, code, name, price, issue_date, sha_hash = cert
        text = (
            f"📜 *COSA NOSTRA PROPERTY CERTIFICATE*\n\n"
            f"ID: `{cert_num}`\n"
            f"Asset: *[{code}] {name}*\n"
            f"Owner ID: `{owner_id}`\n"
            f"Value: {price:,} Koin\n"
            f"Date: {issue_date}\n\n"
            f"SHA-256 Hash Verification:\n`{sha_hash}`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

# ==========================================
# ADMIN & CHEAT COMMANDS
# ==========================================
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 **AKSES DITOLAK:** Anda tidak memiliki otoritas Administrator.")

        async with db.execute("SELECT COUNT(*), SUM(koin) FROM users") as c1:
            total_users, total_circulation = await c1.fetchone()

        text = (
            f"🛠️ *VAULT ADMIN & CHEAT PANEL*\n\n"
            f"Level Otoritas Anda: *Tier {tier}*\n"
            f"Total Pengguna Terdaftar: *{total_users:,}*\n"
            f"Total Koin Beredar: *{total_circulation or 0:,} Koin*\n\n"
            f"*Fitur Admin:*\n"
            f"• `/override_balance [user_id] [jumlah] [alasan]`\n"
            f"• `/broadcast [pesan]`\n"
            f"• `/set_admin [user_id] [tier_0-4]`\n\n"
            f"*Fitur Cheat Admin:*\n"
            f"• `/cheat_koin [jumlah] [target_id]`\n"
            f"• `/cheat_item [kode_item] [target_id]`\n"
            f"• `/cheat_gelar [G1-G7] [target_id]`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_cheat_koin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda bukan Admin!")

        amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 1000000
        target_id = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else user_id

        await db.execute("UPDATE users SET koin = koin + ? WHERE user_id = ?", (amount, target_id))
        await db.commit()

        await update.message.reply_text(f"🧪 **ADMIN CHEAT:** Berhasil menambahkan *+{amount:,} Koin* ke User ID `{target_id}`!", parse_mode="Markdown")

async def cmd_cheat_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda bukan Admin!")

        if not context.args:
            return await update.message.reply_text("❌ Format: `/cheat_item [kode_item] [target_user_id (optional)]`", parse_mode="Markdown")

        code = context.args[0].upper()
        if code not in CATALOG:
            return await update.message.reply_text("❌ Kode item tidak ada di katalog!")

        target_id = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else user_id
        item = CATALOG[code]

        cert_num, sha256_hash, issue_date = generate_certificate(target_id, code, item["name"], 0)
        epoch_now = int(time.time())

        await db.execute(
            "INSERT INTO inventory (user_id, item_code, item_type, cert_number, acquired_at) VALUES (?, ?, ?, ?, ?)",
            (target_id, code, item["type"], cert_num, epoch_now)
        )
        await db.execute(
            "INSERT INTO certificates VALUES (?, ?, ?, ?, ?, ?, ?)",
            (cert_num, target_id, code, item["name"], 0, issue_date, sha256_hash)
        )
        await db.commit()

        await update.message.reply_text(
            f"🧪 **ADMIN CHEAT:** Memunculkan **[{code}] {item['name']}** untuk User ID `{target_id}`!\nID Cert: `{cert_num}`",
            parse_mode="Markdown"
        )

async def cmd_cheat_gelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda bukan Admin!")

        if not context.args:
            return await update.message.reply_text("❌ Format: `/cheat_gelar [G1-G7] [target_user_id (optional)]`", parse_mode="Markdown")

        gelar_code = context.args[0].upper()
        target_id = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else user_id

        await db.execute("UPDATE users SET gelar_tier = ? WHERE user_id = ?", (gelar_code, target_id))
        await db.commit()

        await update.message.reply_text(f"🧪 **ADMIN CHEAT:** Gelar User ID `{target_id}` diubah menjadi **{gelar_code}**!", parse_mode="Markdown")

async def cmd_override_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 2:
            return await update.message.reply_text("🚫 Perintah ini membutuhkan akses **Admin Tier 2**.")

        if len(context.args) < 3 or not context.args[0].isdigit() or not context.args[1].isdigit():
            return await update.message.reply_text("❌ Format: `/override_balance [target_user_id] [jumlah_koin] [alasan]`")

        target_id = int(context.args[0])
        new_balance = int(context.args[1])
        reason = " ".join(context.args[2:])

        await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (new_balance, target_id))
        await db.execute(
            "INSERT INTO admin_logs (admin_id, action, target_id, details, timestamp) VALUES (?, 'OVERRIDE_BALANCE', ?, ?, ?)",
            (user_id, target_id, f"Set balance: {new_balance}. Reason: {reason}", int(time.time()))
        )
        await db.commit()

        await update.message.reply_text(
            f"🛠️ *ADMIN OVERRIDE SUCCESS*\n\nTarget ID: `{target_id}`\nSaldo Baru: *{new_balance:,} Koin*\nAlasan: _{reason}_",
            parse_mode="Markdown"
        )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 3:
            return await update.message.reply_text("🚫 Perintah ini membutuhkan akses **Admin Tier 3**.")

        if not context.args:
            return await update.message.reply_text("❌ Format: `/broadcast [pesan]`")

        broadcast_msg = " ".join(context.args)
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

        success_count = 0
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u[0],
                    text=f"📢 *PENGUMUMAN RESMI COSA NOSTRA*\n\n{broadcast_msg}",
                    parse_mode="Markdown"
                )
                success_count += 1
            except Exception:
                continue

        await update.message.reply_text(f"✅ Terkirim ke *{success_count}/{len(users)}* pengguna.")

async def cmd_set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 4:
            return await update.message.reply_text("🚫 Hanya **Owner (Tier 4)** yang dapat mengubah wewenang Admin.")

        if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
            return await update.message.reply_text("❌ Format: `/set_admin [target_user_id] [tier_0-4]`")

        target_id = int(context.args[0])
        set_tier = int(context.args[1])

        if set_tier < 0 or set_tier > 4:
            return await update.message.reply_text("❌ Level tier tidak valid! Pilih 0 sampai 4.")

        await db.execute("UPDATE users SET admin_tier = ? WHERE user_id = ?", (set_tier, target_id))
        await db.commit()

        await update.message.reply_text(f"👑 **ADMIN AUTHORITY UPDATED:** Target `{target_id}` diset ke **Tier {set_tier}**.")

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Public Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("beli", cmd_beli))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("business", cmd_business))
    app.add_handler(CommandHandler("properties", cmd_portfolio))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("inventory", cmd_portfolio))
    app.add_handler(CommandHandler("certificate", cmd_certificate))

    # Admin & Cheat Commands
    app.add_handler(CommandHandler("admin_panel", cmd_admin_panel))
    app.add_handler(CommandHandler("override_balance", cmd_override_balance))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("set_admin", cmd_set_admin))
    app.add_handler(CommandHandler("cheat_koin", cmd_cheat_koin))
    app.add_handler(CommandHandler("cheat_item", cmd_cheat_item))
    app.add_handler(CommandHandler("cheat_gelar", cmd_cheat_gelar))

    return app

def main():
    import asyncio
    # Inisialisasi DB secara eksplisit sebelum polling dimulai
    asyncio.run(init_db())
    app = build_app()
    print("💰 Telegram Cosa Nostra Vault Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
