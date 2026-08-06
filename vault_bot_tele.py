import os
import aiosqlite
import hashlib
import random
import time
import logging
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler
)

# Set up Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("vault_bot")

# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("TELEGRAM_VAULT_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_VAULT_BOT_TOKEN belum diset di Variables Railway!")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "cosa_nostra.db")
WIB = timezone(timedelta(hours=7))

MY_PERMANENT_OWNER_ID = 8396793986

# Threshold pencapaian ke rank berikutnya (Rank 1 - 6)
RANK_THRESHOLDS = [5, 15, 30, 50, 80, 120]

RANK_TITLES = {
    "police": ["Cadet", "Officer", "Detective", "Sergeant", "Lieutenant", "Captain", "Commissioner"],
    "lawyer": ["Paralegal", "Junior Attorney", "Associate", "Senior Partner", "Managing Partner", "Defense Elite", "Legal Titan"],
    "judge": ["Clerk", "Magistrate", "District Judge", "Circuit Judge", "Appellate Judge", "Supreme Justice", "Chief Justice"],
    "politician": ["Intern", "Campaign Staff", "City Councilor", "State Representative", "Senator", "Governor", "President"],
    "journalist": ["Cub Reporter", "Staff Writer", "Investigative Reporter", "Senior Editor", "Managing Editor", "Bureau Chief", "Media Mogul"]
}

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
                last_business_collect INTEGER DEFAULT 0,
                career_track TEXT DEFAULT 'mafia',
                career_rank INTEGER DEFAULT 0,
                arrest_count INTEGER DEFAULT 0,
                bail_count INTEGER DEFAULT 0,
                pardon_count INTEGER DEFAULT 0,
                expose_count INTEGER DEFAULT 0,
                amnesty_count INTEGER DEFAULT 0
            )
        """)

        columns = [
            ("career_track", "TEXT DEFAULT 'mafia'"),
            ("career_rank", "INTEGER DEFAULT 0"),
            ("arrest_count", "INTEGER DEFAULT 0"),
            ("bail_count", "INTEGER DEFAULT 0"),
            ("pardon_count", "INTEGER DEFAULT 0"),
            ("expose_count", "INTEGER DEFAULT 0"),
            ("amnesty_count", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in columns:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass

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
# GLOBAL ERROR HANDLER
# ==========================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>Terjadi Kesalahan Teknis!</b>\n\n"
            "Sistem baru saja mengalami kendala pemrosesan. Coba ulangi perintah sekali lagi.",
            parse_mode="HTML"
        )

# ==========================================
# DATABASE HELPER & USER MANAGEMENT
# ==========================================
USER_COLUMNS = "user_id, username, koin, bank_balance, bank_loan, vitality, gelar_tier, heat, respect, admin_tier, jailed_until, bounty, crew_id, last_work, last_daily, job_active, job_finish_time, last_business_collect, career_track, career_rank, arrest_count, bail_count, pardon_count, expose_count, amnesty_count"

def get_rank_title(track: str, rank: int) -> str:
    titles = RANK_TITLES.get(track, [])
    if 0 <= rank < len(titles):
        return titles[rank]
    return f"Rank {rank}"

async def check_and_update_rank(db, user_id: int, track: str, current_count: int, current_rank: int) -> tuple[int, bool]:
    new_rank = current_rank
    for i, threshold in enumerate(RANK_THRESHOLDS):
        if current_count >= threshold:
            new_rank = i + 1
        else:
            break
    new_rank = min(new_rank, 6)
    promoted = new_rank > current_rank
    if promoted:
        await db.execute("UPDATE users SET career_rank = ? WHERE user_id = ?", (new_rank, user_id))
        await db.commit()
    return new_rank, promoted

def parse_target_id(context) -> int | None:
    if not context.args or not context.args[0].lstrip("-").isdigit():
        return None
    return int(context.args[0])

async def get_or_create_user(db, user_id: int, username: str):
    """Fungsi standar untuk mengambil/membuat data user & memastikan struktur DB siap."""
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
# KATALOG ITEM & BLACKMARKET (DIPERBAARUI)
# ==========================================
CATALOG = {
    # MAKANAN (F1-F12)
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
    "F11": {"name": "Gelato Sicilian", "type": "food", "price": 400, "vit": 18, "desc": "Es krim segar khas Sisilia"},
    "F12": {"name": "Prosciutto e Melone", "type": "food", "price": 1800, "vit": 38, "desc": "Hidangan pembuka ham & melon"},

    # MINUMAN (D1-D12)
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
    "D11": {"name": "Limoncello Tradizionale", "type": "drink", "price": 900, "vit": 24, "desc": "Liqueur lemon khas Italia Selatan"},
    "D12": {"name": "Cognac Hennessy XO", "type": "drink", "price": 4800, "vit": 50, "desc": "Cognac kelas tinggi beraroma kayu oak"},

    # SENJATA (W1-W14)
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
    "W13": {"name": "Submachine Gun Tommy Gun", "type": "weapon", "price": 32000, "desc": "Senjata ikonik para gangster mafia klasik"},
    "W14": {"name": "Kevlar Stun Baton", "type": "weapon", "price": 6000, "desc": "Tongkat kejut taktis non-lethal penegak hukum"},

    # ARMOR (A1-A12)
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
    "A12": {"name": "Ballistic Shield Elite", "type": "armor", "price": 28000, "desc": "Perisai portabel pelindung regu penyergap"},

    # PERHIASAN (J1-J16)
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
    "J16": {"name": "Black Diamond Brooch", "type": "jewelry", "price": 270000, "desc": "Bros berlian hitam langka"},

    # PROPERTI (H1-H13)
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
    "H13": {"name": "Subterranean Safehouse", "type": "property", "price": 950000, "passive": 11000, "desc": "Bunker rahasia bawah tanah dengan sistem pemantauan otomatis"},

    # KENDARAAN (V1-V15)
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
    "V15": {"name": "Tactical Interceptor Patrol", "type": "vehicle", "price": 350000, "passive": 1400, "desc": "Mobil patroli taktis kecepatan tinggi berspesifikasi khusus"},

    # SERAGAM (S1-S8)
    "S1": {"name": "Casual Street", "type": "suit", "price": 10000, "desc": "Pakaian jalanan biasa"},
    "S2": {"name": "Business Formal", "type": "suit", "price": 25000, "desc": "Jas bisnis formal"},
    "S3": {"name": "Tactical Combat", "type": "suit", "price": 40000, "desc": "Seragam tempur taktis"},
    "S4": {"name": "Tailored Tuxedo", "type": "suit", "price": 60000, "desc": "Tuxedo kustom penjahit"},
    "S5": {"name": "Executive Suit", "type": "suit", "price": 80000, "desc": "Setelan pimpinan eksekutif"},
    "S6": {"name": "Silk Robe Mafia", "type": "suit", "price": 100000, "desc": "Jubah sutra elegan bos"},
    "S7": {"name": "Don Signature Suit", "type": "suit", "price": 150000, "desc": "Setelan khas Godfather"},
    "S8": {"name": "Diplomatic Gala Attire", "type": "suit", "price": 120000, "desc": "Setelan gaun/jas khusus jamuan tinggi pejabat kenegaraan"},

    # GELAR PANGKAT (G1-G7)
    "G1": {"name": "Made Man", "type": "gelar", "price": 5000, "desc": "Tier 1: Anggota resmi keluarga"},
    "G2": {"name": "Enforcer Primus", "type": "gelar", "price": 20000, "desc": "Tier 2: Eksekutor utama"},
    "G3": {"name": "Capo Regime", "type": "gelar", "price": 75000, "desc": "Tier 3: Kapten wilayah"},
    "G4": {"name": "Underboss Executive", "type": "gelar", "price": 250000, "desc": "Tier 4: Wakil pimpinan"},
    "G5": {"name": "Grand Consigliere", "type": "gelar", "price": 500000, "desc": "Tier 5: Penasihat agung"},
    "G6": {"name": "Caporegime Supremo", "type": "gelar", "price": 1000000, "desc": "Tier 6: Komandan tertinggi"},
    "G7": {"name": "Don / Donna Famiglia", "type": "gelar", "price": 2500000, "desc": "Tier 7: Penguasa puncak"},

    # BISNIS (B1-B14)
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
    "B13": {"name": "Syndicate Kartel Global", "type": "business", "price": 1000000, "passive": 20000, "desc": "Konsorsium kriminal dunia"},
    "B14": {"name": "Biro Hukum & Pertanahan", "type": "business", "price": 320000, "passive": 7500, "desc": "Kantor hukum komersial pengelola persetujuan izin bangunan"}
}

CATEGORIES_MAP = {
    "makanan": [f"F{i}" for i in range(1, 13)],
    "minuman": [f"D{i}" for i in range(1, 13)],
    "senjata": [f"W{i}" for i in range(1, 15)],
    "armor": [f"A{i}" for i in range(1, 13)],
    "perhiasan": [f"J{i}" for i in range(1, 17)],
    "properti": [f"H{i}" for i in range(1, 14)],
    "kendaraan": [f"V{i}" for i in range(1, 16)],
    "seragam": [f"S{i}" for i in range(1, 9)],
    "gelar": [f"G{i}" for i in range(1, 8)],
    "bisnis": [f"B{i}" for i in range(1, 15)]
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
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4  

    async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

# ==========================================
# SYSTEM SUB-MENU INTERAKTIF (INLINE KEYBOARD)
# ==========================================
def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🛍️ Katalog Shop", callback_data="vmenu_shop"),
            InlineKeyboardButton("🏦 Bank & Rekening", callback_data="vmenu_bank")
        ],
        [
            InlineKeyboardButton("💼 Bisnis Pasif", callback_data="vmenu_business"),
            InlineKeyboardButton("🎒 Portofolio & Aset", callback_data="vmenu_portfolio")
        ],
        [
            InlineKeyboardButton("💼 Karir & Profesi", callback_data="vmenu_career")
        ],
        [
            InlineKeyboardButton("🛠️ Vault Admin", callback_data="vmenu_admin")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_shop_category_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🍕 Makanan", callback_data="vcat_makanan"),
            InlineKeyboardButton("🍷 Minuman", callback_data="vcat_minuman")
        ],
        [
            InlineKeyboardButton("🔫 Senjata", callback_data="vcat_senjata"),
            InlineKeyboardButton("🛡️ Armor", callback_data="vcat_armor")
        ],
        [
            InlineKeyboardButton("💎 Perhiasan", callback_data="vcat_perhiasan"),
            InlineKeyboardButton("🏠 Properti", callback_data="vcat_properti")
        ],
        [
            InlineKeyboardButton("🏎️ Kendaraan", callback_data="vcat_kendaraan"),
            InlineKeyboardButton("👔 Seragam", callback_data="vcat_seragam")
        ],
        [
            InlineKeyboardButton("🏆 Gelar", callback_data="vcat_gelar"),
            InlineKeyboardButton("🏢 Bisnis", callback_data="vcat_bisnis")
        ],
        [
            InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="vmenu_main")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="vmenu_main")]])

def get_back_to_shop_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Katalog Shop", callback_data="vmenu_shop")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, username)

    text = (
        "🏛️ <b>SELAMAT DATANG DI PUSAT VAULT & PERBANKAN COSA NOSTRA</b>\n"
        "──────────────────────────────────────────\n"
        "<i>\"Honor, Loyalty, and Excellence in Every Transaction.\"</i>\n\n"
        "Selamat datang di Portal Administrasi Finansial & Vault Utama. Kami siap melayani serta memfasilitasi seluruh kebutuhan transaksi perbankan, perolehan aset premium, kepemilikan sertifikasi digital, investigasi pers jurnalis, penerbitan kebijakan amnesti politisi, dan manajemen investasi pasif Anda secara profesional, transparan, dan aman.\n\n"
        "Silakan pilih kategori layanan yang Anda butuhkan melalui tombol interaktif di bawah ini:"
    )
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu_keyboard(), parse_mode="HTML")

async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "vmenu_main":
        await start(update, context)

    elif data == "vmenu_shop":
        text = (
            "🛍️ <b>KATALOG COSA NOSTRA SHOP</b>\n\n"
            "Pilih kategori barang yang ingin kamu lihat dari tombol di bawah ini.\n"
            "Gunakan perintah <code>/beli [kode_item]</code> untuk membeli!"
        )
        await query.edit_message_text(text, reply_markup=get_shop_category_keyboard(), parse_mode="HTML")

    elif data.startswith("vcat_"):
        category = data.replace("vcat_", "")
        if category in CATEGORIES_MAP:
            codes = CATEGORIES_MAP[category]
            text = f"🛍️ <b>KATALOG SHOP ({category.upper()})</b>\n\n"
            for code in codes:
                item = CATALOG[code]
                text += f"• <b>[{code}] {item['name']}</b> — {item['price']:,} Koin\n  <i>{item['desc']}</i>\n\n"
            text += "👉 <i>Gunakan perintah <code>/beli [kode]</code> untuk membeli item!</i>"
            await query.edit_message_text(text, reply_markup=get_back_to_shop_button(), parse_mode="HTML")

    elif data == "vmenu_bank":
        text = (
            "🏦 <b>SUB-MENU LAYANAN BANK & REKENING</b>\n\n"
            "• <code>/bank balance</code> — Cek saldo tunai, bank, & hutang\n"
            "• <code>/bank deposit [jumlah]</code> — Setor uang ke bank (Laundromat Fee 2%)\n"
            "• <code>/bank withdraw [jumlah]</code> — Tarik uang dari bank\n"
            "• <code>/bank loan [jumlah]</code> — Ajukan pinjaman ke bank (Maks: 500k)\n"
            "• <code>/bank payloan [jumlah]</code> — Bayar hutang pinjaman bank"
        )
        await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    elif data == "vmenu_business":
        text = (
            "💼 <b>SUB-MENU BISNIS & INCOME PASIF</b>\n\n"
            "• <code>/business status</code> — Cek unit aset bisnis & pendapatan harian\n"
            "• <code>/business collect</code> — Klaim hasil pendapatan pasif harian (24 jam)"
        )
        await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    elif data == "vmenu_portfolio":
        text = (
            "🎒 <b>SUB-MENU PORTOFOLIO & VERIFIKASI ASET</b>\n\n"
            "• <code>/portfolio</code> — Cek total aset, koleksi barang, & kekayaan\n"
            "• <code>/properties</code> — Cek daftar properti & kendaraan yang dimiliki\n"
            "• <code>/certificate [cert_id]</code> — Verifikasi otentisitas sertifikat aset"
        )
        await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    elif data == "vmenu_career":
        text = (
            "💼 <b>SUB-MENU PROFESI & KARIR PUBLIK</b>\n\n"
            "• <code>/career</code> — Cek status karir & opsi pendaftaran profesi\n"
            "• <code>/badge</code> — Cek progres lencana pangkat profesi\n"
            "• <code>/expose [user_id]</code> — Rilis berita investigasi audit target (Journalist)\n"
            "• <code>/amnesty [user_id]</code> — Terbitkan amnesti pemotongan Heat/Bounty (Politician)"
        )
        await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    elif data == "vmenu_admin":
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            tier = await check_admin_tier(db, user_id)
            if tier == 0:
                return await query.edit_message_text(
                    "🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.",
                    reply_markup=get_back_button(),
                    parse_mode="HTML"
                )

            async with db.execute("SELECT COUNT(*), SUM(koin) FROM users") as c1:
                total_users, total_circulation = await c1.fetchone()

            text = (
                f"🛠️ <b>VAULT ADMIN & CHEAT PANEL</b>\n\n"
                f"Level Otoritas Anda: <b>Tier {tier}</b>\n"
                f"Total Pengguna Terdaftar: <b>{total_users:,}</b>\n"
                f"Total Koin Beredar: <b>{total_circulation or 0:,} Koin</b>\n\n"
                f"<b>Fitur Pengawasan Admin:</b>\n"
                f"• <code>/cek_rekening [target_id]</code> (Tier 1+)\n"
                f"• <code>/audit_user [target_id]</code> (Tier 1+)\n"
                f"• <code>/reset_user [target_id]</code> (Tier 2+)\n\n"
                f"<b>Fitur Admin Lainnya:</b>\n"
                f"• <code>/override_balance [user_id] [jumlah] [alasan]</code> (Tier 2+)\n"
                f"• <code>/broadcast [pesan]</code> (Tier 3+)\n"
                f"• <code>/set_admin [user_id] [tier_0-4]</code> (Tier 4)\n\n"
                f"<b>Fitur Cheat Admin:</b>\n"
                f"• <code>/cheat_koin [jumlah] [target_id]</code> (Tier 1+)\n"
                f"• <code>/cheat_item [kode_item] [target_id]</code> (Tier 1+)\n"
                f"• <code>/cheat_gelar [G1-G7] [target_id]</code> (Tier 1+)"
            )
            await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# PUBLIC COMMAND HANDLERS
# ==========================================
async def cmd_career(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        gelar_tier = user[6]
        track = user[18]
        rank = user[19]
        arrest = user[20]
        bail = user[21]
        pardon = user[22]
        expose = user[23]
        amnesty = user[24]

        if context.args and context.args[0].lower() == "choose":
            if len(context.args) < 2:
                return await update.message.reply_text(
                    "📋 <b>PILIH JALUR KARIR RESMI</b>\n\n"
                    "Opsi jalur karir publik yang tersedia:\n"
                    "• <code>police</code> — Penegak Hukum & Pemburu Buronan\n"
                    "• <code>lawyer</code> — Pengacara & Penjamin Tahanan\n"
                    "• <code>judge</code> — Hakim Agung Pemutus Vonis\n"
                    "• <code>politician</code> — Pejabat Publik & Kebijakan\n"
                    "• <code>journalist</code> — Jurnalis Investigasi & Audit\n\n"
                    "Format: <code>/career choose [track]</code>\n"
                    "<i>Contoh: /career choose journalist</i>",
                    parse_mode="HTML"
                )

            selected = context.args[1].lower()
            if selected not in RANK_TITLES:
                return await update.message.reply_text("❌ Jalur karir tidak valid. Pilih antara: police, lawyer, judge, politician, atau journalist.")

            if gelar_tier != "G0":
                return await update.message.reply_text(
                    "🚫 <b>KONFLIK KEPENTINGAN DITOLAK!</b>\n\n"
                    f"Anda sudah memegang gelar Sindikat Mafia (<b>{gelar_tier}</b>). "
                    "Petinggi kartel tidak dapat merangkap jabatan sebagai Pejabat / Jurnalis Publik!",
                    parse_mode="HTML"
                )

            await db.execute("UPDATE users SET career_track = ?, career_rank = 0 WHERE user_id = ?", (selected, user_id))
            await db.commit()
            
            title = get_rank_title(selected, 0)
            return await update.message.reply_text(
                f"📑 <b>PELANTIKAN KARIR PUBLIK BERHASIL!</b>\n\n"
                f"Jalur Karir  : <b>{selected.upper()}</b>\n"
                f"Pangkat Awal : <b>{title}</b> (Rank 0)\n\n"
                f"Laksanakan tugas operasional Anda untuk menaikkan pangkat!",
                parse_mode="HTML"
            )

        title = get_rank_title(track, rank) if track != "mafia" else f"Mafia ({gelar_tier})"
        count_map = {
            "police": (arrest, "Penangkapan"),
            "lawyer": (bail, "Pembebasan Bail"),
            "judge": (pardon, "Pemberian Ampunan"),
            "politician": (amnesty, "Amnesti Publik"),
            "journalist": (expose, "Laporan Investigasi")
        }
        
        stat_info = ""
        if track in count_map:
            val, label = count_map[track]
            stat_info = f"\n📊 Record Operasional : <b>{val} {label}</b>"

        text = (
            f"💼 <b>PROFIL KARIR & OTORITAS PUBLIK</b>\n"
            f"──────────────────────────────\n"
            f"Jalur Karir  : <b>{track.upper()}</b>\n"
            f"Pangkat/Rank : <b>{title}</b> (Rank {rank}){stat_info}\n"
            f"──────────────────────────────\n"
            f"<i>Gunakan '/career choose [track]' jika ingin beralih profesi (Khusus non-mafia G0).</i>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_badge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        arrest = user[20]
        bail = user[21]
        pardon = user[22]
        expose = user[23]
        amnesty = user[24]

        if track == "mafia":
            return await update.message.reply_text("🍷 Lencana karir publik tidak berlaku untuk jajaran Sindikat Mafia. Naikkan gelar hirarki via Shop!")

        count_map = {
            "police": arrest,
            "lawyer": bail,
            "judge": pardon,
            "politician": amnesty,
            "journalist": expose
        }

        current_count = count_map.get(track, 0)
        title = get_rank_title(track, rank)

        if rank >= 6:
            progress_str = "🏆 <b>RANK MAKSIMAL TERCAPAI (Media Mogul / Titan)</b>"
        else:
            target_next = RANK_THRESHOLDS[rank]
            remaining = target_next - current_count
            next_title = get_rank_title(track, rank + 1)
            progress_str = (
                f"🎯 Next Rank    : <b>{next_title}</b> (Rank {rank + 1})\n"
                f"📈 Progres Target: <b>{current_count}/{target_next}</b> (Sisa {remaining} tindakan lagi)"
            )

        text = (
            f"🎖️ <b>LENCANA INSIGNIA & PROGRES PANGKAT</b>\n"
            f"──────────────────────────────\n"
            f"Profesi      : <b>{track.upper()}</b>\n"
            f"Lencana Aktif: <b>{title}</b> (Rank {rank})\n"
            f"──────────────────────────────\n"
            f"{progress_str}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_expose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khusus Jurnalis: Publikasikan rincian aset & Heat target ke publik."""
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    target_id = parse_target_id(context)

    if not target_id:
        return await update.message.reply_text("Format laporan investigasi: <code>/expose [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        expose_count = user[23]

        if track != "journalist":
            return await update.message.reply_text("🚫 Akses pengeluaran laporan investigasi publik (Expose) hanya dimiliki oleh Jurnalis!")

        async with db.execute("SELECT username, koin, bank_balance, heat, bounty FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("❌ Citizen ID target tidak ditemukan.")

        t_name, t_koin, t_bank, t_heat, t_bounty = target
        royalty = 3000 + (rank * 1200)

        await db.execute("UPDATE users SET koin = koin + ?, expose_count = expose_count + 1 WHERE user_id = ?", (royalty, user_id))
        
        new_expose = expose_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "journalist", new_expose, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('journalist', new_rank)}</b>!" if promoted else ""

        text = (
            f"📰 <b>BERITA UTAMA — INVESTIGASI MEDIA EXPOSE!</b>\n\n"
            f"Subjek Investigasi: <b>@{t_name}</b> (<code>{target_id}</code>)\n"
            f"──────────────────────────────\n"
            f"💵 Likuiditas Cash: <b>{t_koin:,} Koin</b>\n"
            f"🏦 Deposito Bank : <b>{t_bank:,} Koin</b>\n"
            f"🔥 Level Heat   : <b>{t_heat}</b>\n"
            f"🎯 Status Bounty : <b>{t_bounty:,} Koin</b>\n"
            f"──────────────────────────────\n"
            f"🖋️ Royalty Artikel: <b>+{royalty:,} Koin</b>\n"
            f"📊 Total Laporan  : {new_expose} Laporan Terbit{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_amnesty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Khusus Politisi: Menerbitkan amnesties pemotongan Heat & Bounty target."""
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    target_id = parse_target_id(context)

    if not target_id:
        return await update.message.reply_text("Format permohonan amnesties: <code>/amnesty [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        amnesty_count = user[24]

        if track != "politician":
            return await update.message.reply_text("🚫 Otoritas pengeluaran kebijakan Amnesti Publik hanya dimiliki oleh Politisi!")

        async with db.execute("SELECT username, heat, bounty FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("❌ Citizen ID target tidak ditemukan.")

        t_name, t_heat, t_bounty = target

        if t_heat == 0 and t_bounty == 0:
            return await update.message.reply_text("⚠️ Target tidak memiliki Heat atau Bounty aktif untuk diberikan amnesties.")

        # Persentase pemotongan Heat/Bounty berdasarkan rank politisi (20% s/d 80%)
        reduction_rate = 0.20 + (rank * 0.10)
        new_heat = int(t_heat * (1 - reduction_rate))
        new_bounty = int(t_bounty * (1 - reduction_rate))

        await db.execute("UPDATE users SET heat = ?, bounty = ? WHERE user_id = ?", (new_heat, new_bounty, target_id))
        await db.execute("UPDATE users SET amnesty_count = amnesty_count + 1 WHERE user_id = ?", (user_id,))

        new_amnesty = amnesty_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "politician", new_amnesty, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('politician', new_rank)}</b>!" if promoted else ""

        text = (
            f"🏛️ <b>KEBIJAKAN AMNESTI PUBLIK DITERBITKAN!</b>\n\n"
            f"Subjek Penerima : <b>@{t_name}</b> (<code>{target_id}</code>)\n"
            f"📊 Potongan Resiko: <b>{int(reduction_rate * 100)}%</b>\n"
            f"🔥 Heat Baru    : {t_heat} ➔ <b>{new_heat}</b>\n"
            f"🎯 Bounty Baru  : {t_bounty:,} ➔ <b>{new_bounty:,} Koin</b>\n"
            f"🗳️ Rekor Politisi : {new_amnesty} Kebijakan Disahkan{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    category = args[0].lower() if args else None

    if category in CATEGORIES_MAP:
        codes = CATEGORIES_MAP[category]
        text = f"🛍️ <b>KATALOG SHOP ({category.upper()})</b>\n\n"
        for code in codes:
            item = CATALOG[code]
            text += f"• <b>[{code}] {item['name']}</b> — {item['price']:,} Koin\n  <i>{item['desc']}</i>\n\n"
        text += "Gunakan <code>/beli [kode]</code> untuk membeli."
    else:
        text = "🛍️ <b>COSA NOSTRA SHOP CATALOG</b>\n\nGunakan <code>/shop [kategori]</code> untuk memilih:\n\n"
        for cat in CATEGORIES_MAP.keys():
            text += f"• <code>/shop {cat}</code>\n"
    
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_beli(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Masukkan kode item. Contoh: <code>/beli F1</code> atau <code>/beli H1</code>", parse_mode="HTML")
    
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
                    f"❌ <b>PEMBELIAN DITOLAK:</b> Anda sudah pernah membeli gelar <b>{item['name']} ({code})</b>.",
                    parse_mode="HTML"
                )

            current_tier_num = int(current_gelar.replace("G", "")) if current_gelar and current_gelar.startswith("G") else 0
            target_tier_num = int(code.replace("G", ""))

            if target_tier_num <= current_tier_num:
                return await update.message.reply_text(
                    f"❌ <b>PEMBELIAN DITOLAK:</b> Gelar Anda saat ini (<b>{current_gelar}</b>) sudah setara atau lebih tinggi dari <b>{code}</b>.",
                    parse_mode="HTML"
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
                f"🍽️ <b>KONSUMSI BERHASIL</b>\n\n"
                f"Item: <b>[{code}] {item['name']}</b>\n"
                f"Harga: {price:,} Koin\n"
                f"Vitality Bertambah: +{vit_gain}% (Sisa Vitality: <b>{new_vit}%</b>)\n"
                f"Sisa Saldo: {new_koin:,} Koin",
                parse_mode="HTML"
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
        f"✅ <b>TRANSAKSI BERHASIL</b>\n\n"
        f"Item: <b>[{code}] {item['name']}</b>\n"
        f"Harga: {price:,} Koin\n"
        f"Sisa Saldo: {new_koin:,} Koin\n"
        f"Certificate ID: <code>{cert_num}</code>"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, username)
        koin, bank_balance, bank_loan = user[2], user[3], user[4]

        if not args or args[0].lower() == "balance":
            text = (
                f"🏦 <b>REKENING BANK COSA NOSTRA</b>\n\n"
                f"💵 Cash Tunai: <b>{koin:,} Koin</b>\n"
                f"📈 Tabungan Bank: <b>{bank_balance:,} Koin</b>\n"
                f"⚠️ Hutang Pinjaman: <b>{bank_loan:,} Koin</b>\n\n"
                f"Gunakan:\n"
                f"• <code>/bank deposit [jumlah]</code>\n"
                f"• <code>/bank withdraw [jumlah]</code>\n"
                f"• <code>/bank loan [jumlah]</code>\n"
                f"• <code>/bank payloan [jumlah]</code>"
            )
            return await update.message.reply_text(text, parse_mode="HTML")

        action = args[0].lower()
        amount = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0

        if action == "deposit":
            if amount <= 0 or amount > koin:
                return await update.message.reply_text("❌ Jumlah deposit tidak valid!")
            
            fee = int(amount * 0.02)
            net_deposit = amount - fee

            await db.execute("UPDATE users SET koin = koin - ?, bank_balance = bank_balance + ? WHERE user_id = ?", (amount, net_deposit, user_id))
            await db.commit()
            return await update.message.reply_text(
                f"✅ <b>DEPOSIT BERHASIL</b>\n\n"
                f"Jumlah Setoran: {amount:,} Koin\n"
                f"Biaya Pencucian Uang (2%): -{fee:,} Koin\n"
                f"Net Saldo Diterima: <b>+{net_deposit:,} Koin</b>",
                parse_mode="HTML"
            )

        elif action == "withdraw":
            if amount <= 0 or amount > bank_balance:
                return await update.message.reply_text("❌ Saldo bank tidak mencukupi!")
            await db.execute("UPDATE users SET koin = koin + ?, bank_balance = bank_balance - ? WHERE user_id = ?", (amount, amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 Berhasil menarik <b>{amount:,} Koin</b> dari Bank.", parse_mode="HTML")

        elif action == "loan":
            if amount <= 0 or amount > 500000 or bank_loan > 0:
                return await update.message.reply_text("❌ Pinjaman tidak dapat diproses (Maks: 500K koin & tidak boleh ada pinjaman aktif)!")
            await db.execute("UPDATE users SET koin = koin + ?, bank_loan = ? WHERE user_id = ?", (amount, amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"⚠️ <b>PINJAMAN DISETUJUI:</b> Anda menerima <b>{amount:,} Koin</b>.", parse_mode="HTML")

        elif action == "payloan":
            pay_amount = bank_loan if amount == 0 else min(amount, bank_loan)
            if pay_amount <= 0 or koin < pay_amount or bank_loan <= 0:
                return await update.message.reply_text("❌ Pembayaran pinjaman gagal!")
            await db.execute("UPDATE users SET koin = koin - ?, bank_loan = bank_loan - ? WHERE user_id = ?", (pay_amount, pay_amount, user_id))
            await db.commit()
            return await update.message.reply_text(f"✅ Berhasil melunasi pinjaman sebesar <b>{pay_amount:,} Koin</b>.", parse_mode="HTML")

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
            status_txt = "✅ <b>SIAP DIKLAIM!</b> Gunakan <code>/business collect</code>" if rem == 0 else f"⏳ Klaim lagi dalam: {rem//3600}j {(rem%3600)//60}m"
            text = (
                f"💼 <b>STATUS OPERASI BISNIS PASIF</b>\n\n"
                f"Unit Aset: {len(assets)} Unit\n"
                f"Hasil / 24 Jam: <b>+{total_daily_passive:,} Koin</b>\n"
                f"Status: {status_txt}"
            )
            return await update.message.reply_text(text, parse_mode="HTML")

        if args[0].lower() == "collect":
            if total_daily_passive <= 0:
                return await update.message.reply_text("❌ Anda belum memiliki aset bisnis pasif.")
            if now_epoch - last_collect < 86400:
                rem = 86400 - (now_epoch - last_collect)
                return await update.message.reply_text(f"⏳ Klaim baru bisa dilakukan lagi dalam {rem//3600}j {(rem%3600)//60}m.")

            new_koin = user[2] + total_daily_passive
            await db.execute("UPDATE users SET koin = ?, last_business_collect = ? WHERE user_id = ?", (new_koin, now_epoch, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 <b>HASIL BISNIS:</b> Anda mendapatkan <b>+{total_daily_passive:,} Koin</b>.", parse_mode="HTML")

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
            f"🎒 <b>PORTOFOLIO KEKAYAAN & ASET</b>\n"
            f"User: <b>@{db_username}</b>\n"
            f"Cash Tunai: <b>{cash:,} Koin</b>\n"
            f"Saldo Bank: <b>{bank:,} Koin</b>\n"
            f"Gelar Utama: <b>{gelar}</b>\n"
            f"───────────────────\n"
        )

        if not inv:
            text += (
                "📦 <b>Koleksi Aset (0 Item):</b>\n"
                "ℹ️ <i>Anda belum memiliki aset atau properti di Vault.</i>\n\n"
                "💡 <b>Tips:</b> Gunakan perintah <code>/shop</code> untuk melihat katalog barang/aset, lalu beli menggunakan <code>/beli [kode_item]</code>."
            )
        else:
            text += f"📦 <b>Koleksi Aset ({len(inv)} Item):</b>\n"
            for code, cert, acquired_at in inv:
                item_info = CATALOG.get(code, {})
                name = item_info.get("name", code)
                
                date_str = datetime.fromtimestamp(acquired_at, tz=WIB).strftime("%Y-%m-%d") if acquired_at else "N/A"
                text += f"• <b>[{code}] {name}</b>\n  └ Cert: <code>{cert}</code> | <i>{date_str}</i>\n"

        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_certificate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("❌ Masukkan ID Sertifikat. Contoh: <code>/certificate CSN-PROP-...</code>", parse_mode="HTML")

    cert_id = context.args[0]
    async with get_db_connection() as db:
        async with db.execute("SELECT cert_number, user_id, asset_code, asset_name, purchase_price, issue_date, sha256_hash FROM certificates WHERE cert_number = ?", (cert_id,)) as cursor:
            cert = await cursor.fetchone()

        if not cert:
            return await update.message.reply_text("❌ <b>INVALID CERTIFICATE!</b>", parse_mode="HTML")

        cert_num, owner_id, code, name, price, issue_date, sha_hash = cert
        text = (
            f"📜 <b>COSA NOSTRA PROPERTY CERTIFICATE</b>\n\n"
            f"ID: <code>{cert_num}</code>\n"
            f"Asset: <b>[{code}] {name}</b>\n"
            f"Owner ID: <code>{owner_id}</code>\n"
            f"Value: {price:,} Koin\n"
            f"Date: {issue_date}\n\n"
            f"SHA-256 Hash Verification:\n<code>{sha_hash}</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

# ==========================================
# ADMIN INSPECTION & CHEAT DETECTION COMMANDS
# ==========================================
async def cmd_cek_rekening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fitur Admin untuk mengecek seluruh saldo, aset, dan status user secara detail."""
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda butuh wewenang Admin Tier 1+.", parse_mode="HTML")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("❌ Format: <code>/cek_rekening [target_user_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text(f"❌ User ID <code>{target_id}</code> tidak ditemukan di database.", parse_mode="HTML")

        async with db.execute("SELECT item_code FROM inventory WHERE user_id = ?", (target_id,)) as inv_cur:
            items = await inv_cur.fetchall()
        
        total_asset_val = sum([CATALOG.get(code[0], {}).get("price", 0) for code in items])

        t_username = target[1]
        t_koin = target[2]
        t_bank = target[3]
        t_loan = target[4]
        t_vitality = target[5]
        t_gelar = target[6]
        t_heat = target[7]
        t_respect = target[8]
        t_jailed = target[10]
        t_bounty = target[11]

        jail_status = "BEBAS" if t_jailed <= int(time.time()) else f"DIPENJARA ({t_jailed - int(time.time())} detik)"

        text = (
            f"🔍 <b>INSPEKSI REKENING & FINANSIAL USER</b>\n\n"
            f"User ID: <code>{target_id}</code> (@{t_username})\n"
            f"Gelar / Tier: <b>{t_gelar}</b>\n"
            f"Status Hukum: <b>{jail_status}</b>\n"
            f"───────────────────\n"
            f"💵 Cash Tunai: <b>{t_koin:,} Koin</b>\n"
            f"🏦 Saldo Bank: <b>{t_bank:,} Koin</b>\n"
            f"⚠️ Hutang Pinjaman: <b>{t_loan:,} Koin</b>\n"
            f"💎 Total Nilai Aset Shop: <b>{total_asset_val:,} Koin</b>\n"
            f"💰 <b>TOTAL KEKAYAAN BERSIH: {(t_koin + t_bank + total_asset_val - t_loan):,} Koin</b>\n"
            f"───────────────────\n"
            f"⚡ Vitality: {t_vitality}%\n"
            f"🔥 Heat Level: {t_heat}\n"
            f"🏆 Respect: {t_respect}\n"
            f"🎯 Bounty Target: {t_bounty:,} Koin\n"
            f"📦 Total Unit Barang: {len(items)} Item"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_audit_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fitur Admin untuk mendeteksi potensi kecurangan/kejadian tidak wajar."""
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda butuh wewenang Admin Tier 1+.", parse_mode="HTML")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("❌ Format: <code>/audit_user [target_user_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text(f"❌ User ID <code>{target_id}</code> tidak ditemukan.", parse_mode="HTML")

        async with db.execute("SELECT item_code FROM inventory WHERE user_id = ?", (target_id,)) as inv_cur:
            items = await inv_cur.fetchall()

        total_koin = target[2] + target[3]
        total_items = len(items)

        red_flags = []
        if total_koin > 50_000_000:
            red_flags.append("⚠️ Saldo koin melebihi batas wajar (>50 Juta koin)")
        if total_items > 50:
            red_flags.append("⚠️ Memiliki inventaris berlebihan (>50 item)")
        if target[2] > 10_000_000 and target[13] == 0:
            red_flags.append("⚠️ Saldo tunai sangat tinggi tetapi tidak memiliki rekam jejak kerja")

        flag_status = "🚨 <b>DITEMUKAN KEJANGGALAN!</b>" if red_flags else "✅ <b>AKUN NORMAL (BEBAS CURIGA)</b>"
        details_txt = "\n".join(red_flags) if red_flags else "• Tidak ada indikasi cheat yang mencurigakan."

        text = (
            f"🛡️ <b>SYSTEM AUDIT CHEAT DETECTION</b>\n\n"
            f"Target ID: <code>{target_id}</code> (@{target[1]})\n"
            f"Status Hasil Audit: {flag_status}\n\n"
            f"<b>Detail Catatan Sistem:</b>\n{details_txt}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_reset_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fitur Admin Tier 2+ untuk mereset koin & sita aset user yang terbukti ngecheat."""
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 2:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Butuh akses Admin Tier 2+.", parse_mode="HTML")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("❌ Format: <code>/reset_user [target_user_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])

        await db.execute("UPDATE users SET koin = 10000, bank_balance = 0, bank_loan = 0, gelar_tier = 'G0' WHERE user_id = ?", (target_id,))
        await db.execute("DELETE FROM inventory WHERE user_id = ?", (target_id,))
        await db.execute("DELETE FROM certificates WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(
            f"⚖️ <b>SANKSI ADMIN DITERAPKAN!</b>\n\n"
            f"Akun ID <code>{target_id}</code> telah di-reset:\n"
            f"• Saldo dikembalikan ke standar awal (10.000 Koin)\n"
            f"• Seluruh inventaris & sertifikat disita\n"
            f"• Gelar diturunkan ke G0",
            parse_mode="HTML"
        )

# ==========================================
# ADMIN CONTROL & CHEAT COMMANDS
# ==========================================
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.", parse_mode="HTML")

        async with db.execute("SELECT COUNT(*), SUM(koin) FROM users") as c1:
            total_users, total_circulation = await c1.fetchone()

        text = (
            f"🛠️ <b>VAULT ADMIN & CHEAT PANEL</b>\n\n"
            f"Level Otoritas Anda: <b>Tier {tier}</b>\n"
            f"Total Pengguna Terdaftar: <b>{total_users:,}</b>\n"
            f"Total Koin Beredar: <b>{total_circulation or 0:,} Koin</b>\n\n"
            f"<b>Fitur Pengawasan Admin:</b>\n"
            f"• <code>/cek_rekening [target_id]</code>\n"
            f"• <code>/audit_user [target_id]</code>\n"
            f"• <code>/reset_user [target_id]</code>\n\n"
            f"<b>Fitur Admin Lainnya:</b>\n"
            f"• <code>/override_balance [user_id] [jumlah] [alasan]</code>\n"
            f"• <code>/broadcast [pesan]</code>\n"
            f"• <code>/set_admin [user_id] [tier_0-4]</code>\n\n"
            f"<b>Fitur Cheat Admin:</b>\n"
            f"• <code>/cheat_koin [jumlah] [target_id]</code>\n"
            f"• <code>/cheat_item [kode_item] [target_id]</code>\n"
            f"• <code>/cheat_gelar [G1-G7] [target_id]</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_cheat_koin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda bukan Admin!", parse_mode="HTML")

        amount = int(context.args[0]) if context.args and context.args[0].isdigit() else 1000000
        target_id = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else user_id

        await db.execute("UPDATE users SET koin = koin + ? WHERE user_id = ?", (amount, target_id))
        await db.commit()

        await update.message.reply_text(f"🧪 <b>ADMIN CHEAT:</b> Berhasil menambahkan <b>+{amount:,} Koin</b> ke User ID <code>{target_id}</code>!", parse_mode="HTML")

async def cmd_cheat_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda bukan Admin!", parse_mode="HTML")

        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/cheat_item [kode_item] [target_user_id (optional)]</code>", parse_mode="HTML")

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
            f"🧪 <b>ADMIN CHEAT:</b> Memunculkan <b>[{code}] {item['name']}</b> untuk User ID <code>{target_id}</code>!\nID Cert: <code>{cert_num}</code>",
            parse_mode="HTML"
        )

async def cmd_cheat_gelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda bukan Admin!", parse_mode="HTML")

        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/cheat_gelar [G1-G7] [target_user_id (optional)]</code>", parse_mode="HTML")

        gelar_code = context.args[0].upper()
        target_id = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else user_id

        await db.execute("UPDATE users SET gelar_tier = ? WHERE user_id = ?", (gelar_code, target_id))
        await db.commit()

        await update.message.reply_text(f"🧪 <b>ADMIN CHEAT:</b> Gelar User ID <code>{target_id}</code> diubah menjadi <b>{gelar_code}</b>!", parse_mode="HTML")

async def cmd_override_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 2:
            return await update.message.reply_text("🚫 Perintah ini membutuhkan akses <b>Admin Tier 2</b>.", parse_mode="HTML")

        if len(context.args) < 3 or not context.args[0].isdigit() or not context.args[1].isdigit():
            return await update.message.reply_text("❌ Format: <code>/override_balance [target_user_id] [jumlah_koin] [alasan]</code>", parse_mode="HTML")

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
            f"🛠️ <b>ADMIN OVERRIDE SUCCESS</b>\n\nTarget ID: <code>{target_id}</code>\nSaldo Baru: <b>{new_balance:,} Koin</b>\nAlasan: <i>{reason}</i>",
            parse_mode="HTML"
        )

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 3:
            return await update.message.reply_text("🚫 Perintah ini membutuhkan akses <b>Admin Tier 3</b>.", parse_mode="HTML")

        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/broadcast [pesan]</code>", parse_mode="HTML")

        broadcast_msg = " ".join(context.args)
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

        success_count = 0
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u[0],
                    text=f"📢 <b>PENGUMUMAN RESMI COSA NOSTRA</b>\n\n{broadcast_msg}",
                    parse_mode="HTML"
                )
                success_count += 1
            except Exception:
                continue

        await update.message.reply_text(f"✅ Terkirim ke <b>{success_count}/{len(users)}</b> pengguna.", parse_mode="HTML")

async def cmd_set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 4:
            return await update.message.reply_text("🚫 Hanya <b>Owner (Tier 4)</b> yang dapat mengubah wewenang Admin.", parse_mode="HTML")

        if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
            return await update.message.reply_text("❌ Format: <code>/set_admin [target_user_id] [tier_0-4]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        set_tier = int(context.args[1])

        if set_tier < 0 or set_tier > 4:
            return await update.message.reply_text("❌ Level tier tidak valid! Pilih 0 sampai 4.")

        await db.execute("UPDATE users SET admin_tier = ? WHERE user_id = ?", (set_tier, target_id))
        await db.commit()

        await update.message.reply_text(f"👑 <b>ADMIN AUTHORITY UPDATED:</b> Target <code>{target_id}</code> diset ke <b>Tier {set_tier}</b>.", parse_mode="HTML")

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    # Public Navigation & Callback Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^(vmenu_|vcat_)"))

    # Public Commands
    app.add_handler(CommandHandler("shop", cmd_shop))
    app.add_handler(CommandHandler("beli", cmd_beli))
    app.add_handler(CommandHandler("bank", cmd_bank))
    app.add_handler(CommandHandler("business", cmd_business))
    app.add_handler(CommandHandler("properties", cmd_portfolio))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("inventory", cmd_portfolio))
    app.add_handler(CommandHandler("certificate", cmd_certificate))

    # Public Career & Professional Commands
    app.add_handler(CommandHandler("career", cmd_career))
    app.add_handler(CommandHandler("badge", cmd_badge))
    app.add_handler(CommandHandler("expose", cmd_expose))
    app.add_handler(CommandHandler("amnesty", cmd_amnesty))

    # Admin Inspection Commands
    app.add_handler(CommandHandler("cek_rekening", cmd_cek_rekening))
    app.add_handler(CommandHandler("audit_user", cmd_audit_user))
    app.add_handler(CommandHandler("reset_user", cmd_reset_user))

    # Admin Control & Cheat Commands
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
    asyncio.run(init_db())
    app = build_app()
    print("💰 Telegram Cosa Nostra Vault Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
