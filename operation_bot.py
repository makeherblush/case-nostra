import os
import time
import random
import asyncio
import logging
import sqlite3
import aiosqlite
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
logger = logging.getLogger("operation_bot")

# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("TELEGRAM_OPERATIONS_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_OPERATIONS_BOT_TOKEN belum diset di Variables Railway!")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "cosa_nostra.db")
WIB = timezone(timedelta(hours=7)) # UTC+7

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
# HELPER KONEKSI DATABASE (WAL MODE ENABLER)
# ==========================================
@asynccontextmanager
async def get_db_connection():
    db = await aiosqlite.connect(DB_NAME, timeout=30.0)
    try:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        yield db
    finally:
        await db.close()

# ==========================================
# DATABASE HELPER & AUTO-SYNC LOGIC
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

async def sync_gelar_from_assets(db, user_id: int) -> str:
    try:
        async with db.execute(
            """SELECT item_code, item_name FROM assets 
               WHERE user_id = ? AND (item_code LIKE 'CSN-GLR%' OR item_name LIKE '%[G%') 
               ORDER BY item_id DESC LIMIT 1""", 
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                code_or_name = f"{row[0]} {row[1]}"
                new_gelar = "G0"
                if "G7" in code_or_name: new_gelar = "G7"
                elif "G6" in code_or_name: new_gelar = "G6"
                elif "G5" in code_or_name: new_gelar = "G5"
                elif "G4" in code_or_name: new_gelar = "G4"
                elif "G3" in code_or_name: new_gelar = "G3"
                elif "G2" in code_or_name: new_gelar = "G2"
                elif "G1" in code_or_name: new_gelar = "G1"

                if new_gelar != "G0":
                    await db.execute("UPDATE users SET gelar_tier = ? WHERE user_id = ?", (new_gelar, user_id))
                    await db.commit()
                    return new_gelar
    except Exception:
        pass 
    
    async with db.execute("SELECT gelar_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
        return res[0] if res else "G0"

async def get_or_create_user(db, user_id: int, username: str):
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
    await db.commit()

    async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
        user = await cursor.fetchone()
        
    if user:
        if user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()
            
        await sync_gelar_from_assets(db, user_id)
        
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()
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
# MATRIX JOB & CRIMES
# ==========================================
JOBS = {
    "debt": {"name": "Debt Collector", "tier": "G1", "category": "mafia", "dur": 3600, "vit": 15, "min": 400, "max": 800, "desc": "Kumpulin utang dari debitur lokal"},
    "smuggle_s": {"name": "Small Smuggling", "tier": "G1", "category": "mafia", "dur": 7200, "vit": 20, "min": 1000, "max": 2000, "desc": "Selundupkan barang kecil"},
    "pickpocket": {"name": "Pickpocket", "tier": "G1", "category": "mafia", "dur": 3600, "vit": 10, "min": 500, "max": 1200, "desc": "Curi dompet warga"},
    "muscle": {"name": "Street Muscle", "tier": "G1", "category": "mafia", "dur": 5400, "vit": 18, "min": 600, "max": 1000, "desc": "Intimidasi target di jalanan"},
    "drug_run": {"name": "Drug Runner", "tier": "G1", "category": "mafia", "dur": 10800, "vit": 25, "min": 1500, "max": 3000, "desc": "Antar paket narkoba ke distributor"},

    "blackmail": {"name": "Blackmail Specialist", "tier": "G2", "category": "mafia", "dur": 7200, "vit": 20, "min": 2000, "max": 4000, "desc": "Ancam & peras target rentan"},
    "protection": {"name": "Protected Territory", "tier": "G2", "category": "mafia", "dur": 14400, "vit": 30, "min": 2500, "max": 5000, "desc": "Pungut uang perlindungan pertokoan"},
    "sabotage": {"name": "Sabotage Operation", "tier": "G2", "category": "mafia", "dur": 10800, "vit": 25, "min": 3000, "max": 6000, "desc": "Rusak fasilitas & aset musuh"},
    "interrogate": {"name": "Interrogation", "tier": "G2", "category": "mafia", "dur": 7200, "vit": 22, "min": 1500, "max": 3500, "desc": "Interogasi paksa target"},
    "arms_trade": {"name": "Arms Trading", "tier": "G2", "category": "mafia", "dur": 10800, "vit": 28, "min": 4000, "max": 7000, "desc": "Jual beli senjata gelap"},

    "conquer": {"name": "Territorial Conquest", "tier": "G3", "category": "mafia", "dur": 18000, "vit": 35, "min": 5000, "max": 10000, "desc": "Ambil alih wilayah baru"},
    "contract": {"name": "Hit Job Contract", "tier": "G3", "category": "mafia", "dur": 14400, "vit": 30, "min": 6000, "max": 12000, "desc": "Eksekusi kontrak pembunuhan"},
    "heist_plan": {"name": "Heist Planning", "tier": "G3", "category": "mafia", "dur": 21600, "vit": 40, "min": 7000, "max": 15000, "desc": "Rencanakan perampokan besar"},

    "corrupt": {"name": "Government Corruption", "tier": "G4", "category": "mafia", "dur": 18000, "vit": 35, "min": 10000, "max": 20000, "desc": "Suap pejabat publik"},
    "bank_heist": {"name": "Major Bank Heist", "tier": "G4", "category": "mafia", "dur": 25200, "vit": 50, "min": 15000, "max": 30000, "desc": "Rampok bank nasional"},

    "policy": {"name": "National Policy Control", "tier": "G5", "category": "mafia", "dur": 21600, "vit": 40, "min": 20000, "max": 40000, "desc": "Kendalikan kebijakan hukum"},
    "traffic": {"name": "International Trafficking", "tier": "G5", "category": "mafia", "dur": 28800, "vit": 50, "min": 25000, "max": 50000, "desc": "Penyelundupan internasional"},

    "topple": {"name": "Topple Government", "tier": "G6", "category": "mafia", "dur": 43200, "vit": 60, "min": 60000, "max": 120000, "desc": "Gulingkan rezim pemerintahan"},
    "ultimate": {"name": "Ultimate World Domination", "tier": "G7", "category": "mafia", "dur": 108000, "vit": 80, "min": 200000, "max": 400000, "desc": "Kuasai tatanan dunia baru"},

    "patrol": {"name": "Rondaan Patroli Kota", "tier": "G0", "category": "police", "dur": 3600, "vit": 10, "min": 600, "max": 1200, "desc": "Patroli ketertiban jalanan umum"},
    "cop_bribe": {"name": "Polisi Terlibat Suap (Korupsi)", "tier": "G1", "category": "police", "dur": 7200, "vit": 15, "min": 1500, "max": 3500, "desc": "Terima uang damai dari bos kriminal jalanan"},
    "undercover": {"name": "Penyusupan Intel Undercover", "tier": "G2", "category": "police", "dur": 10800, "vit": 25, "min": 3000, "max": 7000, "desc": "Menyamar sebagai anggota sindikat Cosa Nostra"},
    "raid_lab": {"name": "Penggerebekan Lab Gelap", "tier": "G3", "category": "police", "dur": 14400, "vit": 35, "min": 6000, "max": 14000, "desc": "Serbu gudang penyimpan aset narkoba mafia"},
    "mole_leak": {"name": "Bocorkan Informasi Intel (Mole)", "tier": "G4", "category": "police", "dur": 18000, "vit": 40, "min": 12000, "max": 25000, "desc": "Jual jadwal sergap polisi ke pihak Famiglia"},
    "chief_protection": {"name": "Pelindung Ring Atas (Chief)", "tier": "G5", "category": "police", "dur": 28800, "vit": 50, "min": 25000, "max": 60000, "desc": "Hapus catatan kriminal & lindungi operasional kartel"}
}

CRIMES = {
    "pickpocket": {"min": 500, "max": 1500, "risk": 20, "heat": 5, "jail": 3600, "desc": "Curi dompet pejalan kaki"},
    "shoplift": {"min": 800, "max": 2000, "risk": 25, "heat": 8, "jail": 3600, "desc": "Gasak barang pertokoan"},
    "carjack": {"min": 2000, "max": 5000, "risk": 40, "heat": 20, "jail": 7200, "desc": "Curi mobil mewah"},
    "bank_rob": {"min": 10000, "max": 25000, "risk": 60, "heat": 50, "jail": 28800, "desc": "Perampokan bank lokal"},
    "assassination": {"min": 20000, "max": 50000, "risk": 75, "heat": 80, "jail": 43200, "desc": "Pembunuhan berencana VIP"}
}

# ==========================================
# DATABASE INITIALIZATION
# ==========================================
async def init_db():
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crews (
                crew_id INTEGER PRIMARY KEY AUTOINCREMENT,
                crew_name TEXT UNIQUE,
                leader_id INTEGER,
                treasury INTEGER DEFAULT 0,
                respect INTEGER DEFAULT 0,
                created_at INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS assets (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                item_code TEXT,
                item_name TEXT,
                bought_at INTEGER
            )
        """)
        await db.commit()

async def post_init(application):
    await init_db()

async def check_admin_tier(db, user_id: int) -> int:
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4  

    async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

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
# SYSTEM SUB-MENU INTERAKTIF (INLINE KEYBOARD)
# ==========================================
def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("👤 Profil & Status", callback_data="opmenu_profile"),
            InlineKeyboardButton("🔨 Pekerjaan & Misi", callback_data="opmenu_jobs")
        ],
        [
            InlineKeyboardButton("💼 Profesi Publik", callback_data="opmenu_career"),
            InlineKeyboardButton("🎯 Target & Buronan", callback_data="opmenu_targets")
        ],
        [
            InlineKeyboardButton("🏴‍☠️ Organisasi Crew", callback_data="opmenu_crew"),
            InlineKeyboardButton("📖 Kamus Istilah Game", callback_data="opmenu_dictionary")
        ],
        [
            InlineKeyboardButton("🛠️ Operations Admin", callback_data="opmenu_admin")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"

    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, current_username)

    text = (
        "⚔️ <b>SELAMAT DATANG DI PUSAT OPERASIONAL COSA NOSTRA</b>\n"
        "──────────────────────────────────────────\n"
        "<i>\"Loyalty, Strategy, and Supreme Command in Every Operation.\"</i>\n\n"
        "Selamat datang di Portal Komando Utama Operasional Cosa Nostra Network. Bot ini didesain 100% ramah pemula, kamu bisa menjelajah seluruh sistem hanya dengan menekan tombol navigasi di bawah ini.\n\n"
        "💡 <b>PANDUAN MULAI UNTUK PEMULA:</b>\n"
        "1. Klik tombol <b>👤 Profil & Status</b> untuk mengklaim modal koin harian pertamamu.\n"
        "2. Kumpulkan uang tunai dengan memilih menu <b>🔨 Pekerjaan & Misi</b>.\n"
        "3. Bingung dengan istilah di game ini? Tekan tombol <b>📖 Kamus Istilah Game</b> di bawah.\n\n"
        "Silakan pilih kategori operasional yang ingin kamu buka:"
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

    if data == "opmenu_main":
        await start(update, context)

    elif data == "opmenu_profile":
        text = (
            "👤 <b>PUSAT KELOLA PROFIL & STATUS ANGGOTA</b>\n"
            "──────────────────────────────────────────\n"
            "Menu ini membantumu memantau kondisi fisik, dompet, dan status hukum karaktermu secara lengkap.\n\n"
            "<b>Penjelasan Detail Sub-Fitur:</b>\n"
            "• <b>Cek Rekening & Profil:</b> Melihat data KTP-mu, sisa koin tunai, tabungan aman di bank, stamina, dan apakah kamu sedang bebas atau ditahan polisi.\n"
            "• <b>Klaim Tunjangan Harian:</b> Mengambil jatah koin gratis sebesar 2.000 koin setiap 24 jam sekali sebagai modal awal bermain.\n"
            "• <b>Suap Petugas (Bribe):</b> Membayar uang damai/suap kepada aparat. Jika kamu punya status buron (Heat) atau terlanjur mendekam di penjara, fitur ini akan membebaskanmu seketika.\n\n"
            "<i>Tekan tombol di bawah untuk memilih aksi:</i>"
        )
        keyboard = [
            [InlineKeyboardButton("💵 Cek Rekening & Profil Saya", callback_data="opaction_rekening_exec")],
            [InlineKeyboardButton("🎁 Klaim Tunjangan Harian Gratis", callback_data="opaction_daily_exec")],
            [InlineKeyboardButton("💵 Suap Petugas (Bebaskan Heat/Penjara)", callback_data="opaction_bribe_exec")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "opmenu_jobs":
        text = (
            "🔨 <b>PUSAT PEKERJAAN & MISI INTRIK</b>\n"
            "──────────────────────────────────────────\n"
            "Di sinilah tempat utama kamu mencari koin tunai untuk memperkaya karaktermu.\n\n"
            "<b>Penjelasan Detail Sub-Fitur:</b>\n"
            "• <b>Bekerja Harian:</b> Bekerja rutin setiap 1 jam sekali. Setiap kali bekerja, staminamu berkurang 15%. Semakin tinggi pangkat mafiamu (G1-G7), gajimu akan makin besar.\n"
            "• <b>Misi Job Bertingkat:</b> Menjalankan penugasan strategis via tombol interaktif tanpa teks menumpuk.\n"
            "• <b>Aksi Kejahatan Jalanan:</b> Melakukan aksi kejahatan instan via tombol langsung.\n\n"
            "<i>Tekan tombol di bawah untuk mulai beraksi:</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🔨 Bekerja Sekarang (Kerja Harian)", callback_data="opaction_work_exec")],
            [InlineKeyboardButton("💼 Pilih & Jalankan Misi Job", callback_data="opaction_job_menu")],
            [InlineKeyboardButton("🕵️ Pilih & Eksekusi Aksi Kejahatan", callback_data="opaction_crime_menu")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ==========================================
    # SUB-MENU BUTTON: JOB SELECTION
    # ==========================================
    elif data == "opaction_job_menu":
        text = (
            "💼 <b>PILIH MISI STRATEGIS JOB</b>\n"
            "──────────────────────────────────────────\n"
            "Pilih salah satu penugasan di bawah ini sesuai kualifikasi gelar mafiamu:\n\n"
            "<i>Klik tombol misi di bawah untuk langsung memulainya:</i>"
        )
        keyboard = []
        for code, j in JOBS.items():
            btn_text = f"[{j['tier']}] {j['name']} ({j['dur']//3600}j)"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"oprunjob_{code}")])
        keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("oprunjob_"):
        job_code = data.replace("oprunjob_", "")
        user_id = update.effective_user.id
        current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
        now_epoch = int(time.time())

        async with get_db_connection() as db:
            user = await get_or_create_user(db, user_id, current_username)
            active_job = user[15]
            job_finish = user[16]

            if user[10] > now_epoch:
                return await query.edit_message_text("🔒 Anda dalam penjara! Tidak dapat mengeksekusi misi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]), parse_mode="HTML")

            if active_job and job_finish > 0:
                if now_epoch < job_finish:
                    rem = job_finish - now_epoch
                    return await query.edit_message_text(
                        f"⏳ Sedang menjalankan <b>{JOBS[active_job]['name']}</b>. Selesai dalam <b>{rem//3600}j {(rem%3600)//60}m {rem%60}s</b>.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]),
                        parse_mode="HTML"
                    )
                else:
                    j_info = JOBS[active_job]
                    
                    if j_info.get("category") == "police" and random.randint(1, 100) <= 20:
                        jail_until = now_epoch + 7200
                        await db.execute(
                            "UPDATE users SET jailed_until = ?, job_active = NULL, job_finish_time = 0 WHERE user_id = ?",
                            (jail_until, user_id)
                        )
                        await db.commit()
                        return await query.edit_message_text(
                            f"🚨 <b>PENYAMARAN TERBONGKAR!</b>\n\n"
                            f"Identitas Anda saat menjalankan <b>{j_info['name']}</b> terendus oleh pembunuh bayaran Cosa Nostra! "
                            f"Anda berhasil selamat namun ditahan dalam sel isolasi selama 2 jam.",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]),
                            parse_mode="HTML"
                        )

                    reward = random.randint(j_info["min"], j_info["max"])
                    heat_modifier = -5 if j_info.get("category") == "police" else 10

                    await db.execute(
                        "UPDATE users SET koin = koin + ?, heat = MAX(0, heat + ?), job_active = NULL, job_finish_time = 0 WHERE user_id = ?",
                        (reward, heat_modifier, user_id)
                    )
                    await db.commit()

                    return await query.edit_message_text(
                        f"🎉 <b>MISI STRATEGIS BERHASIL!</b>\n\n"
                        f"Anda menyelesaikan <b>{j_info['name']}</b> dan memperoleh imbalan <b>+{reward:,} Koin</b>!",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")]]),
                        parse_mode="HTML"
                    )

            if job_code not in JOBS:
                return await query.edit_message_text("❌ Kode Job tidak ditemukan!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]), parse_mode="HTML")

            j = JOBS[job_code]
            user_gelar = user[6]
            user_tier_num = int(user_gelar.replace("G", "")) if user_gelar.startswith("G") else 0
            req_tier_num = int(j["tier"].replace("G", ""))

            if user_tier_num < req_tier_num:
                return await query.edit_message_text(f"🔒 <b>AKSES DITOLAK:</b> Misi ini membutuhkan minimal gelar <b>{j['tier']}</b>!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]), parse_mode="HTML")

            if user[5] < j["vit"]:
                return await query.edit_message_text(f"⚡ Vitality tidak cukup! Membutuhkan {j['vit']}% Vitality.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_job_menu")]]), parse_mode="HTML")

            finish_time = now_epoch + j["dur"]
            new_vit = user[5] - j["vit"]

            await db.execute(
                "UPDATE users SET vitality = ?, job_active = ?, job_finish_time = ? WHERE user_id = ?",
                (new_vit, job_code, finish_time, user_id)
            )
            await db.commit()

            await query.edit_message_text(
                f"🚀 <b>MISI DIMULAI: {j['name']}</b>\n\n"
                f"Kategori Misi: <b>{j.get('category', 'umum').upper()}</b>\n"
                f"Durasi: {j['dur']//3600} Jam\n"
                f"Proyeksi Hasil: {j['min']:,} - {j['max']:,} Koin\n\n"
                f"Sistem sedang berjalan secara otomatis.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")]]),
                parse_mode="HTML"
            )

    # ==========================================
    # SUB-MENU BUTTON: CRIME SELECTION
    # ==========================================
    elif data == "opaction_crime_menu":
        text = (
            "🕵️ <b>PILIH AKSI KEJAHATAN TAKTIS</b>\n"
            "──────────────────────────────────────────\n"
            "Pilih aksi kejahatan langsung di bawah ini (Hasil instan, namun berisiko masuk sel):\n\n"
        )
        keyboard = []
        for code, c in CRIMES.items():
            btn_text = f"🎭 {c['desc']} ({c['risk']}% Risiko)"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"opruncrime_{code}")])
        keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("opruncrime_"):
        crime_code = data.replace("opruncrime_", "")
        user_id = update.effective_user.id
        current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
        now_epoch = int(time.time())

        async with get_db_connection() as db:
            user = await get_or_create_user(db, user_id, current_username)

            if user[10] > now_epoch:
                return await query.edit_message_text("🔒 Anda sedang mendekam di sel penjara!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_crime_menu")]]), parse_mode="HTML")

            if crime_code not in CRIMES:
                return await query.edit_message_text("❌ Kode aksi kejahatan tidak valid.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="opaction_crime_menu")]]), parse_mode="HTML")

            c = CRIMES[crime_code]
            user_heat = user[7]
            
            effective_risk = min(95, c["risk"] + int(user_heat * 0.5))

            if random.randint(1, 100) <= effective_risk:
                jail_until = now_epoch + c["jail"]
                fine = c["min"] // 2

                await db.execute(
                    "UPDATE users SET koin = MAX(0, koin - ?), heat = heat + ?, jailed_until = ? WHERE user_id = ?",
                    (fine, c["heat"], jail_until, user_id)
                )
                await db.commit()

                return await query.edit_message_text(
                    f"🚨 <b>AKSI GAGAL! TERTANGKAP POLISI</b>\n\n"
                    f"Risiko Efektif Terdeteksi: <b>{effective_risk}%</b> (Heat Penalty: +{int(user_heat * 0.5)}%)\n"
                    f"Denda Disita: -{fine:,} Koin\n"
                    f"Heat Bertambah: +{c['heat']}\n"
                    f"Mendekam di Penjara: {c['jail']//3600} Jam",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")]]),
                    parse_mode="HTML"
                )

            loot = random.randint(c["min"], c["max"])
            await db.execute(
                "UPDATE users SET koin = koin + ?, heat = heat + ?, respect = respect + 10 WHERE user_id = ?",
                (loot, c["heat"] // 2, user_id)
            )
            await db.commit()

            await query.edit_message_text(
                f"🎭 <b>AKSI KEJAHATAN SUKSES!</b>\n\n"
                f"Hasil Rampokan: <b>+{loot:,} Koin</b>\n"
                f"Respect Bertambah: +10\n"
                f"Heat Level: +{c['heat']//2}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Pekerjaan", callback_data="opmenu_jobs")]]),
                parse_mode="HTML"
            )

    elif data == "opmenu_career":
        text = (
            "💼 <b>PUSAT KARIR & PROFESI PUBLIK</b>\n"
            "──────────────────────────────────────────\n"
            "Jika kamu tidak memegang gelar mafia aktif (Gelar G0), kamu bisa bekerja di jalur pemerintahan atau penegak hukum publik.\n\n"
            "<b>Penjelasan Detail 5 Jalur Profesi:</b>\n"
            "1. <b>Police (Kepolisian):</b> Bertugas ronda dan menangkap pemain kriminal ber-Heat tinggi di jalanan.\n"
            "2. <b>Lawyer (Pengacara):</b> Bertugas membayar jaminan untuk membebaskan pemain lain yang sedang dipenjara.\n"
            "3. <b>Judge (Hakim):</b> Memiliki kekuasaan mengeluarkan vonis grasi untuk menghapus catatan kriminal pemain lain.\n"
            "4. <b>Journalist (Jurnalis):</b> Membongkar dan mempublikasikan rahasia kekayaan serta catatan Heat pemain lain ke publik.\n"
            "5. <b>Politician (Politisi):</b> Menerbitkan kebijakan diskon amnesties pemotongan tingkat buron.\n\n"
            "<i>Tekan tombol di bawah untuk mengelola profesimu:</i>"
        )
        keyboard = [
            [InlineKeyboardButton("📋 Cek Profil Karir Saya", callback_data="opaction_career_exec")],
            [InlineKeyboardButton("📑 Pilih / Ganti Jalur Karir", callback_data="opaction_choose_career_menu")],
            [InlineKeyboardButton("🎖️ Cek Lencana Pangkat", callback_data="opaction_badge_exec")],
            [InlineKeyboardButton("🚔 Jalankan Patroli Polisi", callback_data="opaction_patrol_exec")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ==========================================
    # SUB-MENU BUTTON: CAREER SELECTION
    # ==========================================
    elif data == "opaction_choose_career_menu":
        text = (
            "📋 <b>PILIH JALUR KARIR RESMI</b>\n"
            "──────────────────────────────────────────\n"
            "Silakan pilih salah satu jalur karir publik di bawah ini menggunakan tombol interaktif:\n\n"
            "• <b>Police</b> — Penegak Hukum & Pemburu Buronan\n"
            "• <b>Lawyer</b> — Pengacara & Penjamin Tahanan\n"
            "• <b>Judge</b> — Hakim Agung Pemutus Vonis\n"
            "• <b>Politician</b> — Pejabat Publik & Kebijakan\n"
            "• <b>Journalist</b> — Jurnalis Investigasi & Audit\n\n"
            "<i>Catatan: Khusus pemain non-mafia (Gelar G0).</i>"
        )
        keyboard = [
            [InlineKeyboardButton("👮 Police", callback_data="opsetcareer_police"), InlineKeyboardButton("⚖️ Lawyer", callback_data="opsetcareer_lawyer")],
            [InlineKeyboardButton("⚖️ Judge", callback_data="opsetcareer_judge"), InlineKeyboardButton("🏛️ Politician", callback_data="opsetcareer_politician")],
            [InlineKeyboardButton("📰 Journalist", callback_data="opsetcareer_journalist")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Karir", callback_data="opmenu_career")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("opsetcareer_"):
        selected = data.replace("opsetcareer_", "")
        user_id = update.effective_user.id
        current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
        
        async with get_db_connection() as db:
            user = await get_or_create_user(db, user_id, current_username)
            gelar_tier = user[6]

            if gelar_tier != "G0":
                return await query.edit_message_text(
                    "🚫 <b>KONFLIK KEPENTINGAN DITOLAK!</b>\n\n"
                    f"Anda sudah memegang gelar Sindikat Mafia (<b>{gelar_tier}</b>). "
                    "Petinggi kartel tidak dapat merangkap jabatan sebagai Penegak Hukum publik!",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Karir", callback_data="opmenu_career")]]),
                    parse_mode="HTML"
                )

            await db.execute("UPDATE users SET career_track = ?, career_rank = 0 WHERE user_id = ?", (selected, user_id))
            await db.commit()
            
            title = get_rank_title(selected, 0)
            await query.edit_message_text(
                f"📑 <b>PELANTIKAN KARIR PUBLIK BERHASIL!</b>\n\n"
                f"Jalur Karir  : <b>{selected.upper()}</b>\n"
                f"Pangkat Awal : <b>{title}</b> (Rank 0)\n\n"
                f"Laksanakan tugas operasional Anda untuk menaikkan pangkat!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Karir", callback_data="opmenu_career")]]),
                parse_mode="HTML"
            )

    elif data == "opmenu_targets":
        text = (
            "🎯 <b>PUSAT TARGET & KONTRAK BURONAN</b>\n"
            "──────────────────────────────────────────\n"
            "Menu interaksi dan pertempuran antar sesama pemain di dalam server.\n\n"
            "<b>Penjelasan Detail Sub-Fitur:</b>\n"
            "• <b>Daftar Buronan:</b> Melihat papan peringkat 10 pemain yang memiliki nilai sayembara (Bounty) tertinggi di kota.\n"
            "• <b>Eksekusi Target (/hit):</b> Menyerang pemain lain. Jika menang, kamu akan merampas setengah uang tunai di dompetnya + mengklaim bointynya. Jika gagal, kamu yang akan masuk penjara.\n"
            "• <b>Pasang Bounty (/bounty):</b> Menaruh hadiah koin atas kepala pemain lain agar dipburu dan diserang oleh pemain se-server.\n\n"
            "<i>Tekan tombol di bawah untuk navigasi:</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🚔 Lihat Daftar Buronan Aktif", callback_data="opaction_wanted_exec")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "opmenu_crew":
        text = (
            "🏴‍☠️ <b>PUSAT ORGANISASI CREW (KELOMPOK)</b>\n"
            "──────────────────────────────────────────\n"
            "Sistem perkumpulan/Geng untuk bermain bersama teman-temanmu di Cosa Nostra.\n\n"
            "<b>Penjelasan Detail Sub-Fitur:</b>\n"
            "• <b>Info Crew:</b> Mengecek nama gengmu, nama ketua, total poin reputasi (Respect), dan saldo tabungan kas bersama.\n"
            "• <b>Buat Crew:</b> Mendirikan organisasi geng baru dengan modal investasi 50.000 koin.\n"
            "• <b>Donasi Kas:</b> Menyisihkan uang pribadi untuk disetor ke kas bersama (Treasury) agar gengmu makin kuat.\n\n"
            "<i>Tekan tombol di bawah untuk melihat info crew:</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🏴‍☠️ Cek Info Crew Saya", callback_data="opaction_crew_exec")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "opmenu_dictionary":
        text = (
            "📖 <b>KAMUS ISTILAH PENTING COSA NOSTRA</b>\n"
            "──────────────────────────────────────────\n"
            "Bingung dengan kata-kata di dalam game? Berikut penjelasannya:\n\n"
            "💵 <b>Cash / Koin Tunai:</b> Uang yang sedang kamu pegang di dompet. Uang ini bisa hilang atau dirampok jika kamu diserang pemain lain.\n"
            "🏦 <b>Tabungan Bank:</b> Uang yang kamu simpan aman di bank. Uang di bank tidak bisa dirampok oleh siapa pun.\n"
            "⚡ <b>Vitality (Stamina):</b> Tenaga fisik karaktermu (0-100%). Jika di bawah 20%, kamu terlalu lelah untuk bekerja dan harus membeli makanan.\n"
            "🔥 <b>Heat Level:</b> Tingkat buron kecurigaan polisi padamu. Semakin tinggi Heat, semakin mudah kamu tertangkap dan dijebloskan ke penjara.\n"
            "🏆 <b>Respect:</b> Poin reputasi dan kehormatan karakter atau gengmu di dunia mafia.\n"
            "🎯 <b>Bounty:</b> Uang hadiah/sayembara yang terpasang pada kepala seorang buronan."
        )
        await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    elif data == "opmenu_admin":
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            tier = await check_admin_tier(db, user_id)
            if tier == 0:
                return await query.edit_message_text(
                    "🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator Operations.",
                    reply_markup=get_back_button(),
                    parse_mode="HTML"
                )

            text = (
                f"🛠️ <b>OPERATIONS ADMIN PANEL & MODERASI</b>\n"
                f"──────────────────────────────────────────\n"
                f"Level Otoritas Anda: <b>Tier {tier}</b>\n\n"
                f"Menu khusus pengurus untuk mengaudit player, memenjarakan pelanggar aturan, serta menguji coba fitur via sistem cheat operasional."
            )
            await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    # ==========================================
    # HANDLER ACTION EXECUTION VIA BUTTONS
    # ==========================================
    elif data == "opaction_rekening_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_rekening(fake_update, context)
    elif data == "opaction_daily_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_daily(fake_update, context)
    elif data == "opaction_bribe_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_bribe(fake_update, context)
    elif data == "opaction_work_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_work(fake_update, context)
    elif data == "opaction_career_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        context.args = []
        await cmd_career(fake_update, context)
    elif data == "opaction_badge_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_badge(fake_update, context)
    elif data == "opaction_patrol_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_patrol(fake_update, context)
    elif data == "opaction_wanted_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        await cmd_wanted(fake_update, context)
    elif data == "opaction_crew_exec":
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        context.args = []
        await cmd_crew(fake_update, context)

# ==========================================
# PUBLIC COMMAND HANDLERS
# ==========================================
async def cmd_rekening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        
        db_username = user[1]
        koin = user[2]
        bank_balance = user[3]
        vitality = user[5]
        gelar = user[6]
        heat = user[7]
        respect = user[8]
        jailed_until = user[10]
        bounty = user[11]
        crew_id = user[12]
        career_track = user[18]
        career_rank = user[19]

        jail_status = "🟢 BEBAS"
        if jailed_until > now_epoch:
            rem = jailed_until - now_epoch
            jail_status = f"🔴 PENJARA ({rem//3600}j {(rem%3600)//60}m)"

        crew_name = "Tidak Ada"
        if crew_id > 0:
            async with db.execute("SELECT crew_name FROM crews WHERE crew_id = ?", (crew_id,)) as c_cur:
                c_row = await c_cur.fetchone()
                if c_row:
                    crew_name = c_row[0]

        career_title = get_rank_title(career_track, career_rank) if career_track != "mafia" else f"Mafia ({gelar})"

        text = (
            f"👤 <b>PROFIL ANGGOTA COSA NOSTRA</b>\n\n"
            f"Nama: <b>@{db_username}</b> (<code>{user_id}</code>)\n"
            f"Gelar Pangkat: <b>{gelar}</b>\n"
            f"Profesi Publik: <b>{career_track.upper()}</b> — {career_title}\n"
            f"Crew: <b>{crew_name}</b>\n"
            f"───────────────────\n"
            f"💵 Cash Tunai: <b>{koin:,} Koin</b>\n"
            f"🏦 Tabungan Bank: <b>{bank_balance:,} Koin</b>\n"
            f"⚡ Vitality: <b>{vitality}%</b>\n"
            f"🔥 Heat Level: <b>{heat}</b>\n"
            f"🏆 Respect: <b>{respect}</b>\n"
            f"🎯 Bounty Target: <b>{bounty:,} Koin</b>\n"
            f"Status Hukum: {jail_status}"
        )
        
        await update.message.reply_text(text, parse_mode="HTML")

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
                    "<i>Contoh: /career choose police</i>",
                    parse_mode="HTML"
                )

            selected = context.args[1].lower()
            if selected not in RANK_TITLES:
                return await update.message.reply_text("❌ Jalur karir tidak valid. Pilih antara: police, lawyer, judge, politician, atau journalist.")

            if gelar_tier != "G0":
                return await update.message.reply_text(
                    "🚫 <b>KONFLIK KEPENTINGAN DITOLAK!</b>\n\n"
                    f"Anda sudah memegang gelar Sindikat Mafia (<b>{gelar_tier}</b>). "
                    "Petinggi kartel tidak dapat merangkap jabatan sebagai Penegak Hukum publik!",
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
            f"<i>Gunakan menu interaktif karir untuk beralih profesi secara praktis (Khusus non-mafia G0).</i>"
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

async def cmd_patrol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        arrest_count = user[20]

        if track != "police":
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Perintah patroli unit hanya dapat dieksekusi oleh Kepolisian (Track Police)!")

        now_epoch = int(time.time())

        async with db.execute(
            """SELECT user_id, username, heat, bounty FROM users 
               WHERE (heat >= 30 OR bounty > 0) AND jailed_until < ? AND user_id != ? 
               ORDER BY heat DESC LIMIT 1""",
            (now_epoch, user_id)
        ) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("🚔 <b>PATROLI UNIT KEPOLISIAN:</b> Jalanan kota aman. Tidak ditemukan buronan ber-heat tinggi saat ini.")

        t_id, t_name, t_heat, t_bounty = target
        jail_duration = 300 + (t_heat * 10)
        jailed_until = now_epoch + jail_duration
        commission = 3000 + (rank * 1500) + t_bounty

        await db.execute("UPDATE users SET jailed_until = ?, heat = 0, bounty = 0 WHERE user_id = ?", (jailed_until, t_id))
        await db.execute("UPDATE users SET koin = koin + ?, arrest_count = arrest_count + 1 WHERE user_id = ?", (commission, user_id))
        
        new_arrest = arrest_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "police", new_arrest, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('police', new_rank)}</b>!" if promoted else ""

        text = (
            f"🚨 <b>PATROLI KEPOLISIAN BERHASIL!</b>\n\n"
            f"Unit Patroli menangkap buronan : <b>@{t_name}</b> (<code>{t_id}</code>)\n"
            f"⚖️ Heat Target : {t_heat} | Bounty Ditutup: {t_bounty:,} Koin\n"
            f"🔒 Masa Tahanan: {jail_duration // 60} Menit di Penjara\n"
            f"💵 Imbalan Dinas: <b>+{commission:,} Koin</b>{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_arrest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    target_id = parse_target_id(context)

    if not target_id:
        return await update.message.reply_text("Format penangkapan: <code>/arrest [user_id_buronan]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        arrest_count = user[20]

        if track != "police":
            return await update.message.reply_text("🚫 Wewenang penangkapan manual hanya dimiliki oleh Kepolisian!")

        now_epoch = int(time.time())

        async with db.execute("SELECT username, heat, bounty, jailed_until FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("❌ Citizen ID target tidak ditemukan.")

        t_name, t_heat, t_bounty, t_jailed = target

        if t_jailed > now_epoch:
            return await update.message.reply_text("⚠️ Target sudah mendekam di dalam sel tahanan!")

        if t_heat < 20 and t_bounty <= 0:
            return await update.message.reply_text("⚖️ Target tidak memiliki Heat/Bounty yang cukup untuk penangkapan resmi (Min. 20 Heat).")

        jail_duration = 300 + (t_heat * 12)
        jailed_until = now_epoch + jail_duration
        reward = 2500 + (rank * 1000) + t_bounty

        await db.execute("UPDATE users SET jailed_until = ?, heat = 0, bounty = 0 WHERE user_id = ?", (jailed_until, target_id))
        await db.execute("UPDATE users SET koin = koin + ?, arrest_count = arrest_count + 1 WHERE user_id = ?", (reward, user_id))
        
        new_arrest = arrest_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "police", new_arrest, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('police', new_rank)}</b>!" if promoted else ""

        text = (
            f"⚖️ <b>EKSEKUSI SURAT PENANGKAPAN!</b>\n\n"
            f"Tahanan    : <b>@{t_name}</b> (<code>{target_id}</code>)\n"
            f"🔒 Sel Tahanan: {jail_duration // 60} Menit\n"
            f"💵 Komisi Dinas: <b>+{reward:,} Koin</b>{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_bail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    target_id = parse_target_id(context)

    if not target_id:
        return await update.message.reply_text("Format penjaminan: <code>/bail [user_id_tahanan]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        bail_count = user[21]
        user_koin = user[2]

        if track != "lawyer":
            return await update.message.reply_text("🚫 Jasa penjaminan pembebasan tahanan (Bail) hanya dapat dilakukan oleh Pengacara (Lawyer)!")

        now_epoch = int(time.time())

        async with db.execute("SELECT username, jailed_until FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("❌ Citizen ID target tidak ditemukan.")

        t_name, t_jailed = target

        if t_jailed <= now_epoch:
            return await update.message.reply_text("⚠️ Target saat ini tidak sedang mendekam di penjara.")

        base_bail_fee = 5000
        discount = rank * 600
        final_fee = max(1000, base_bail_fee - discount)

        if user_koin < final_fee:
            return await update.message.reply_text(f"❌ Saldo Anda kurang untuk membayar jaminan Bail sebesar <b>{final_fee:,} Koin</b>.", parse_mode="HTML")

        await db.execute("UPDATE users SET jailed_until = 0 WHERE user_id = ?", (target_id,))
        await db.execute("UPDATE users SET koin = koin - ?, bail_count = bail_count + 1 WHERE user_id = ?", (final_fee, user_id))
        
        new_bail = bail_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "lawyer", new_bail, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('lawyer', new_rank)}</b>!" if promoted else ""

        text = (
            f"⚖️ <b>SURAT PENJAMINAN BAIL DITERBITKAN!</b>\n\n"
            f"Klien Bebas : <b>@{t_name}</b> (<code>{target_id}</code>)\n"
            f"💵 Biaya Bail : <b>{final_fee:,} Koin</b> (Diskon Rank: -{discount:,})\n"
            f"📂 Rekor Lawyer: {new_bail} Kasus Dimenangkan{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_pardon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    target_id = parse_target_id(context)

    if not target_id:
        return await update.message.reply_text("Format grasi: <code>/pardon [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        track = user[18]
        rank = user[19]
        pardon_count = user[22]

        if track != "judge":
            return await update.message.reply_text("🚫 Hak pengeluaran Vonis Ampunan (Pardon) secara mutlak hanya milik Hakim Agung (Judge)!")

        async with db.execute("SELECT username, heat, bounty FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text("❌ Citizen ID target tidak ditemukan.")

        t_name, t_heat, t_bounty = target

        if t_heat == 0 and t_bounty == 0:
            return await update.message.reply_text("⚠️ Target tidak memiliki catatan kriminal / Heat aktif.")

        await db.execute("UPDATE users SET heat = 0, bounty = 0 WHERE user_id = ?", (target_id,))
        await db.execute("UPDATE users SET pardon_count = pardon_count + 1 WHERE user_id = ?", (user_id,))
        
        new_pardon = pardon_count + 1
        new_rank, promoted = await check_and_update_rank(db, user_id, "judge", new_pardon, rank)
        await db.commit()

        promo_msg = f"\n🎉 <b>PROMOSI PANGKAT!</b> Selamat, Anda naik pangkat ke <b>{get_rank_title('judge', new_rank)}</b>!" if promoted else ""

        text = (
            f"⚖️ <b>VONIS AMNESTI & GRASI HAKIM AGUNG!</b>\n\n"
            f"Terdakwa     : <b>@{t_name}</b> (<code>{target_id}</code>)\n"
            f"📜 Keputusan  : Seluruh Heat & Bounty Dihapuskan Resmi\n"
            f"👨‍⚖️ Total Grasi : {new_pardon} Kasus{promo_msg}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_expose(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        
        if user[10] > now_epoch:
            return await update.message.reply_text("🔒 Anda sedang dalam sel penjara! Tidak dapat bekerja.")

        if user[5] < 20:
            return await update.message.reply_text("⚡ Vitality Anda terlalu rendah (<20%)! Beli makanan di Vault Bot (<code>/shop makanan</code>).", parse_mode="HTML")

        last_work = user[13]
        if now_epoch - last_work < 3600:
            rem = 3600 - (now_epoch - last_work)
            return await update.message.reply_text(f"⏳ Istirahat dulu! Bekerja lagi dalam <b>{rem//60}m {rem%60}s</b>.", parse_mode="HTML")

        gelar = user[6]
        base_pay = random.randint(500, 1200)
        gelar_bonus = {"G1": 300, "G2": 800, "G3": 2000, "G4": 5000, "G5": 10000, "G6": 15000, "G7": 25000}.get(gelar, 0)
        total_income = base_pay + gelar_bonus

        new_vit = max(0, user[5] - 15)

        await db.execute(
            "UPDATE users SET koin = koin + ?, vitality = ?, last_work = ? WHERE user_id = ?",
            (total_income, new_vit, now_epoch, user_id)
        )
        await db.commit()

        await update.message.reply_text(
            f"🔨 <b>PENUGASAN SELESAI!</b>\n\n"
            f"Gaji Dasar: +{base_pay:,} Koin\n"
            f"Bonus Gelar ({gelar}): +{gelar_bonus:,} Koin\n"
            f"Total Diterima: <b>+{total_income:,} Koin</b>\n"
            f"Vitality Terkuras: -15% (Sisa: {new_vit}%)",
            parse_mode="HTML"
        )

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        last_daily = user[14]

        if now_epoch - last_daily < 86400:
            rem = 86400 - (now_epoch - last_daily)
            return await update.message.reply_text(f"⏳ Klaim harian berikutnya dalam <b>{rem//3600}j {(rem%3600)//60}m</b>.", parse_mode="HTML")

        reward = 2000
        await db.execute("UPDATE users SET koin = koin + ?, last_daily = ? WHERE user_id = ?", (reward, now_epoch, user_id))
        await db.commit()

        await update.message.reply_text(f"🎁 <b>TUNJANGAN HARIAN:</b> Anda mendapatkan <b>+{reward:,} Koin</b>!", parse_mode="HTML")

async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"

    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, current_username)

    text = (
        "💼 <b>PILIH MISI STRATEGIS JOB</b>\n"
        "──────────────────────────────────────────\n"
        "Gunakan tombol di bawah untuk memilih & menjalankan misi tanpa perlu mengetik kode:"
    )
    keyboard = []
    for code, j in JOBS.items():
        btn_text = f"[{j['tier']}] {j['name']} ({j['dur']//3600}j)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"oprunjob_{code}")])
    keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cmd_crime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"

    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, current_username)

    text = (
        "🕵️ <b>PILIH AKSI KEJAHATAN TAKTIS</b>\n"
        "──────────────────────────────────────────\n"
        "Gunakan tombol di bawah untuk mengeksekusi aksi kejahatan langsung:"
    )
    keyboard = []
    for code, c in CRIMES.items():
        btn_text = f"🎭 {c['desc']} ({c['risk']}% Risiko)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"opruncrime_{code}")])
    keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="opmenu_main")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cmd_bribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        koin = user[2]
        jailed_until = user[10]
        heat = user[7]

        if jailed_until > now_epoch:
            bribe_cost = 50000
            if koin < bribe_cost:
                return await update.message.reply_text(f"❌ Saldo Koin tidak cukup untuk menyuap sipir! Biaya: <b>{bribe_cost:,} Koin</b>", parse_mode="HTML")

            await db.execute("UPDATE users SET koin = koin - ?, jailed_until = 0 WHERE user_id = ?", (bribe_cost, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 <b>SUAP BEBAS PENJARA BERHASIL:</b> Anda membayar <b>{bribe_cost:,} Koin</b> dan langsung dibebaskan!", parse_mode="HTML")

        elif heat > 0:
            bribe_cost = heat * 1000
            if koin < bribe_cost:
                return await update.message.reply_text(f"❌ Saldo Koin tidak cukup untuk menyuap aparat! Biaya pembersihan Heat: <b>{bribe_cost:,} Koin</b>", parse_mode="HTML")

            await db.execute("UPDATE users SET koin = koin - ?, heat = 0 WHERE user_id = ?", (bribe_cost, user_id))
            await db.commit()
            return await update.message.reply_text(f"💵 <b>SUAP BEBAS HEAT BERHASIL:</b> Membayar <b>{bribe_cost:,} Koin</b>. Heat Level kembali ke 0!", parse_mode="HTML")

        else:
            return await update.message.reply_text("ℹ️ Anda tidak dalam penjara dan Heat Level Anda sudah 0.")

async def cmd_bounty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"

    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        return await update.message.reply_text("❌ Format: <code>/bounty [target_user_id] [jumlah_koin]</code>", parse_mode="HTML")

    target_id = int(args[0])
    bounty_amount = int(args[1])

    if target_id == user_id:
        return await update.message.reply_text("❌ Anda tidak bisa memasang bounty pada diri sendiri!")

    if bounty_amount < 1000:
        return await update.message.reply_text("❌ Minimal pasang bounty adalah 1.000 Koin!")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        if user[2] < bounty_amount:
            return await update.message.reply_text("❌ Saldo Koin Anda tidak cukup!")

        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (target_id,)) as t_cur:
            target = await t_cur.fetchone()
            if not target:
                return await update.message.reply_text("❌ Target User ID tidak ditemukan di Database.")

        await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (bounty_amount, user_id))
        await db.execute("UPDATE users SET bounty = bounty + ? WHERE user_id = ?", (bounty_amount, target_id))
        await db.commit()

    await update.message.reply_text(f"🎯 <b>KONTRAK DIPASANG:</b> Sayembara sebesar <b>{bounty_amount:,} Koin</b> terpasang untuk kepala ID <code>{target_id}</code>!", parse_mode="HTML")

async def cmd_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    if not args or not args[0].isdigit():
        return await update.message.reply_text("❌ Format: <code>/hit [target_user_id]</code>", parse_mode="HTML")

    target_id = int(args[0])
    if target_id == user_id:
        return await update.message.reply_text("❌ Anda tidak bisa menyerang diri sendiri.")

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        
        if user[10] > now_epoch:
            return await update.message.reply_text("🔒 Anda dalam penjara!")

        async with db.execute("SELECT user_id, koin, bounty FROM users WHERE user_id = ?", (target_id,)) as t_cur:
            target = await t_cur.fetchone()
            if not target:
                return await update.message.reply_text("❌ Target tidak ditemukan!")

        target_koin = target[1]
        target_bounty = target[2]

        if random.randint(1, 100) <= 40:
            stolen_cash = target_koin // 2
            total_prize = stolen_cash + target_bounty

            await db.execute("UPDATE users SET koin = koin + ?, respect = respect + 25 WHERE user_id = ?", (total_prize, user_id))
            await db.execute("UPDATE users SET koin = koin - ?, bounty = 0 WHERE user_id = ?", (stolen_cash, target_id))
            await db.commit()

            return await update.message.reply_text(
                f"☠️ <b>EKSEKUSI HIT BERHASIL!</b>\n\n"
                f"Target <code>{target_id}</code> berhasil dieksekusi!\n"
                f"Koin Dirampok: {stolen_cash:,} Koin\n"
                f"Bounty Diklaim: {target_bounty:,} Koin\n"
                f"Total Hasil: <b>+{total_prize:,} Koin</b>",
                parse_mode="HTML"
            )
        else:
            jail_until = now_epoch + 7200
            await db.execute("UPDATE users SET jailed_until = ? WHERE user_id = ?", (jail_until, user_id))
            await db.commit()

            return await update.message.reply_text("❌ <b>HIT GAGAL!</b> Target meloloskan diri dan Anda dijebloskan ke penjara selama 2 jam.", parse_mode="HTML")

async def cmd_wanted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT user_id, username, bounty FROM users WHERE bounty > 0 ORDER BY bounty DESC LIMIT 10") as cursor:
            wanted_list = await cursor.fetchall()

        if not wanted_list:
            return await update.message.reply_text("🕊️ Saat ini tidak ada kontrak buronan aktif.")

        text = "🚔 <b>DAFTAR BURONAN COSA NOSTRA</b>\n\n"
        for idx, (u_id, name, b_val) in enumerate(wanted_list, 1):
            text += f"{idx}. <b>@{name}</b> (<code>{u_id}</code>) - 🎯 <b>{b_val:,} Koin</b>\n"

        text += "\nGunakan <code>/hit [user_id]</code> untuk mengeksekusi buronan."
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_crew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or update.effective_user.first_name or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        user_crew_id = user[12]

        if not args:
            if user_crew_id == 0:
                text = (
                    "🏴‍☠️ <b>SISTEM ORGANISASI CREW</b>\n\n"
                    "Anda belum bergabung dengan crew apa pun.\n"
                    "• <code>/crew create [nama_crew]</code> - Buat Crew baru (Biaya: 50.000 Koin)\n"
                    "• <code>/crew join [nama_crew]</code> - Bergabung dengan Crew"
                )
            else:
                async with db.execute("SELECT crew_name, treasury, respect FROM crews WHERE crew_id = ?", (user_crew_id,)) as c_cur:
                    c_info = await c_cur.fetchone()
                text = (
                    f"🏴‍☠️ <b>CREW ANDA: {c_info[0]}</b>\n\n"
                    f"💰 Kas Treasury: <b>{c_info[1]:,} Koin</b>\n"
                    f"🏆 Crew Respect: <b>{c_info[2]}</b>\n\n"
                    f"Command:\n"
                    f"• <code>/crew donate [jumlah]</code> - Salurkan kas ke treasury"
                )
            return await update.message.reply_text(text, parse_mode="HTML")

        action = args[0].lower()

        if action == "create":
            if user_crew_id > 0:
                return await update.message.reply_text("❌ Anda sudah memiliki Crew!")
            if len(args) < 2:
                return await update.message.reply_text("❌ Masukkan nama crew!")

            crew_name = " ".join(args[1:])
            cost = 50000
            if user[2] < cost:
                return await update.message.reply_text(f"❌ Biaya pembuatan Crew adalah {cost:,} Koin!")

            await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (cost, user_id))
            cursor = await db.execute(
                "INSERT INTO crews (crew_name, leader_id, created_at) VALUES (?, ?, ?)",
                (crew_name, user_id, now_epoch)
            )
            new_crew_id = cursor.lastrowid
            await db.execute("UPDATE users SET crew_id = ? WHERE user_id = ?", (new_crew_id, user_id))
            await db.commit()

            return await update.message.reply_text(f"🏴‍☠️ <b>CREW BERHASIL DIBUAT:</b> Selamat mendirikan <b>{crew_name}</b>!", parse_mode="HTML")

        elif action == "donate":
            if user_crew_id == 0:
                return await update.message.reply_text("❌ Anda belum memiliki Crew.")
            if len(args) < 2 or not args[1].isdigit():
                return await update.message.reply_text("❌ Masukkan jumlah donasi!")

            donate_amount = int(args[1])
            if user[2] < donate_amount:
                return await update.message.reply_text("❌ Saldo Koin Anda tidak cukup!")

            await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (donate_amount, user_id))
            await db.execute("UPDATE crews SET treasury = treasury + ? WHERE crew_id = ?", (donate_amount, user_crew_id))
            await db.commit()

            return await update.message.reply_text(f"✅ Berhasil mendonasikan <b>{donate_amount:,} Koin</b> ke Treasury Crew.", parse_mode="HTML")

# ==========================================
# ADMIN INSPECTION & CONTROL COMMANDS
# ==========================================
async def cmd_cek_rekening_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 1+.")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("❌ Format: <code>/cek_rekening [target_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text(f"❌ Target User ID <code>{target_id}</code> tidak ditemukan.", parse_mode="HTML")

        jail_status = "BEBAS" if target[10] <= int(time.time()) else f"PENJARA ({target[10] - int(time.time())}d)"

        text = (
            f"🔍 <b>OPERATIONS INSPECTION PANEL</b>\n\n"
            f"Target ID: <code>{target_id}</code> (@{target[1]})\n"
            f"Gelar Pangkat: <b>{target[6]}</b>\n"
            f"Status Hukum: <b>{jail_status}</b>\n"
            f"───────────────────\n"
            f"💵 Cash Tunai: <b>{target[2]:,} Koin</b>\n"
            f"🏦 Saldo Bank: <b>{target[3]:,} Koin</b>\n"
            f"⚡ Vitality: <b>{target[5]}%</b>\n"
            f"🔥 Heat Level: <b>{target[7]}</b>\n"
            f"🏆 Respect: <b>{target[8]}</b>\n"
            f"🎯 Bounty: <b>{target[11]:,} Koin</b>\n"
            f"🏴‍☠️ Crew ID: <b>{target[12]}</b>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_audit_ops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 1+.")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("❌ Format: <code>/audit_ops [target_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (target_id,)) as cursor:
            target = await cursor.fetchone()

        if not target:
            return await update.message.reply_text(f"❌ Target ID <code>{target_id}</code> tidak ada.", parse_mode="HTML")

        flags = []
        if target[7] > 200:
            flags.append("⚠️ Heat Level sangat tinggi (>200)")
        if target[8] > 5000 and target[13] == 0:
            flags.append("⚠️ Respect sangat tinggi tanpa rekam jejak kerja")
        if target[2] > 20_000_000:
            flags.append("⚠️ Memiliki saldo tunai melimpah (>20 Juta koin)")

        status = "🚨 <b>TERLIHAT MENCURIGAKAN!</b>" if flags else "✅ <b>AKUN NORMAL</b>"
        details = "\n".join(flags) if flags else "• Tidak ada anomali aktivitas operasi."

        text = (
            f"🛡️ <b>SYSTEM OPERATIONAL AUDIT</b>\n\n"
            f"Target ID: <code>{target_id}</code> (@{target[1]})\n"
            f"Hasil Evaluasi: {status}\n\n"
            f"<b>Catatan Audit:</b>\n{details}"
        )
        await update.message.reply_text(text, parse_mode="HTML")

# ==========================================
# ADMIN & CHEAT COMMANDS
# ==========================================
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.", parse_mode="HTML")

        text = (
            f"🛠️ <b>OPERATIONS ADMIN & CHEAT PANEL</b>\n\n"
            f"Level Otoritas Anda: <b>Tier {tier}</b>\n\n"
            f"<b>Fitur Pengawasan Admin:</b>\n"
            f"• <code>/cek_rekening [target_id]</code>\n"
            f"• <code>/audit_ops [target_id]</code>\n\n"
            f"<b>Fitur Admin Operasional:</b>\n"
            f"• <code>/jail_user [user_id] [jam]</code>\n"
            f"• <code>/unjail_user [user_id]</code>\n"
            f"• <code>/clear_heat [user_id]</code>\n\n"
            f"<b>Fitur Cheat Admin:</b>\n"
            f"• <code>/cheat_godmode [target_id]</code>\n"
            f"• <code>/cheat_instant_work [target_id]</code>\n"
            f"• <code>/cheat_clear_bounty [target_id]</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_cheat_godmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda tidak memiliki akses Admin!", parse_mode="HTML")

        await db.execute(
            "UPDATE users SET vitality = 100, heat = 0, jailed_until = 0 WHERE user_id = ?",
            (target_id,)
        )
        await db.commit()

        await update.message.reply_text(
            f"🧪 <b>GODMODE ACTIVATED!</b>\n\nTarget ID: <code>{target_id}</code>\n⚡ Vitality: <b>100%</b>\n🔥 Heat Level: <b>0</b>\n🔓 Status Hukum: <b>BEBAS PENJARA</b>",
            parse_mode="HTML"
        )

async def cmd_cheat_instant_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda tidak memiliki akses Admin!", parse_mode="HTML")

        await db.execute(
            "UPDATE users SET last_work = 0, last_daily = 0, job_finish_time = 1 WHERE user_id = ?",
            (target_id,)
        )
        await db.commit()

        await update.message.reply_text(
            f"🧪 <b>INSTANT RESET ACTIVATED!</b>\n\nTarget ID: <code>{target_id}</code>\n• Cooldown <code>/work</code> & <code>/daily</code> di-reset ke 0!\n• Job aktif siap diklaim sekarang (<code>/job</code>).",
            parse_mode="HTML"
        )

async def cmd_cheat_clear_bounty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda tidak memiliki akses Admin!", parse_mode="HTML")

        await db.execute("UPDATE users SET bounty = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🧪 <b>ADMIN CHEAT:</b> Bounty pada User ID <code>{target_id}</code> telah dibersihkan!", parse_mode="HTML")

async def cmd_jail_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 1+.")

        if len(context.args) < 2:
            return await update.message.reply_text("❌ Format: <code>/jail_user [target_id] [jam]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        hours = int(context.args[1])
        jail_time = int(time.time()) + (hours * 3600)

        await db.execute("UPDATE users SET jailed_until = ? WHERE user_id = ?", (jail_time, target_id))
        await db.commit()

        await update.message.reply_text(f"🔒 Target <code>{target_id}</code> berhasil dipenjara selama <b>{hours} jam</b>.", parse_mode="HTML")

async def cmd_unjail_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/unjail_user [target_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        await db.execute("UPDATE users SET jailed_until = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🔓 Target <code>{target_id}</code> telah dibebaskan dari penjara.", parse_mode="HTML")

async def cmd_clear_heat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 3:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 3+.")

        if not context.args:
            return await update.message.reply_text("❌ Format: <code>/clear_heat [target_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        await db.execute("UPDATE users SET heat = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🔥 Heat Level target <code>{target_id}</code> diset kembali ke 0.", parse_mode="HTML")

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    # Public Navigation & Callback Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^(opmenu_|opaction_|oprunjob_|opruncrime_|opsetcareer_)"))

    # Public Operations Commands
    app.add_handler(CommandHandler("rekening", cmd_rekening))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(CommandHandler("crime", cmd_crime))
    app.add_handler(CommandHandler("bribe", cmd_bribe))
    app.add_handler(CommandHandler("bounty", cmd_bounty))
    app.add_handler(CommandHandler("hit", cmd_hit))
    app.add_handler(CommandHandler("wanted", cmd_wanted))
    app.add_handler(CommandHandler("crew", cmd_crew))

    # Public Career & Law Enforcement Commands
    app.add_handler(CommandHandler("career", cmd_career))
    app.add_handler(CommandHandler("badge", cmd_badge))
    app.add_handler(CommandHandler("patrol", cmd_patrol))
    app.add_handler(CommandHandler("arrest", cmd_arrest))
    app.add_handler(CommandHandler("bail", cmd_bail))
    app.add_handler(CommandHandler("pardon", cmd_pardon))
    app.add_handler(CommandHandler("expose", cmd_expose))
    app.add_handler(CommandHandler("amnesty", cmd_amnesty))

    # Admin Inspection Commands
    app.add_handler(CommandHandler("cek_rekening", cmd_cek_rekening_ops))
    app.add_handler(CommandHandler("cek_user", cmd_cek_rekening_ops))
    app.add_handler(CommandHandler("audit_ops", cmd_audit_ops))

    # Admin Control & Cheat Commands
    app.add_handler(CommandHandler("admin_panel", cmd_admin_panel))
    app.add_handler(CommandHandler("jail_user", cmd_jail_user))
    app.add_handler(CommandHandler("unjail_user", cmd_unjail_user))
    app.add_handler(CommandHandler("clear_heat", cmd_clear_heat))
    app.add_handler(CommandHandler("reset_heat", cmd_clear_heat))
    app.add_handler(CommandHandler("cheat_godmode", cmd_cheat_godmode))
    app.add_handler(CommandHandler("cheat_instant_work", cmd_cheat_instant_work))
    app.add_handler(CommandHandler("cheat_clear_bounty", cmd_cheat_clear_bounty))

    return app

def main():
    import asyncio
    asyncio.run(init_db())
    app = build_app()
    print("⚔️ Telegram Cosa Nostra Operations Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
