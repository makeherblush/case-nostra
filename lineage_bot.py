import os
import time
import random
import hashlib
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
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

# Set up Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("lineage_bot")

# ==========================================
# CONFIGURATION
# ==========================================
TOKEN = os.getenv("TELEGRAM_LINEAGE_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_LINEAGE_BOT_TOKEN belum diset di Variables Railway!")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "cosa_nostra.db")
WIB = timezone(timedelta(hours=7))  # UTC+7

PROPOSAL_TTL_SECONDS = 10 * 60          # lamaran hangus setelah 10 menit
MAX_PROPOSALS_PER_DAY = 3               # anti-spam harass ke banyak target
PROPOSAL_EXPIRY_CHECK_INTERVAL = 60     # background loop cek tiap 60 detik

# State untuk ConversationHandler Register KTP
REG_NAMA, REG_MUSE, REG_UMUR, REG_TGLLAHIR = range(4)

# State untuk ConversationHandler Admin Edit KTP
ADMIN_EDIT_CHOICE, ADMIN_EDIT_VALUE = range(4, 6)

BLACKLISTED_FAMILY_NAMES = {
    "ADMIN", "ADMINISTRATOR", "OFFICIAL", "SYSTEM", "MOD", "MODERATOR",
    "COSA NOSTRA", "COSA_NOSTRA", "COSA NOSTRA OFFICIAL", "OWNER", "STAFF",
}

MY_PERMANENT_OWNER_ID = 8396793986  

# ==========================================
# HELPER KONEKSI DATABASE
# ==========================================
@asynccontextmanager
async def get_db_connection():
    db = await aiosqlite.connect(DB_NAME, timeout=30.0)
    try:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA foreign_keys=ON;")
        yield db
    finally:
        await db.close()

# ==========================================
# UNIVERSAL DATABASE SCHEMA & AUTO MIGRATION
# ==========================================
async def ensure_all_tables_exist(db):
    """Failsafe Universal: Menjamin seluruh tabel dasar selalu ada sebelum transaksi dilakukan."""
    try:
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
                nama_lengkap TEXT DEFAULT 'Warga Anonim',
                muse TEXT DEFAULT 'Tidak Ada',
                umur INTEGER DEFAULT 18,
                tanggal_lahir TEXT DEFAULT '01-01-2000',
                status_sipil TEXT DEFAULT 'Lajang'
            )
        """)

        user_columns = [
            ("bank_balance", "INTEGER DEFAULT 0"),
            ("bank_loan", "INTEGER DEFAULT 0"),
            ("vitality", "INTEGER DEFAULT 100"),
            ("gelar_tier", "TEXT DEFAULT 'G0'"),
            ("heat", "INTEGER DEFAULT 0"),
            ("respect", "INTEGER DEFAULT 0"),
            ("admin_tier", "INTEGER DEFAULT 0"),
            ("jailed_until", "INTEGER DEFAULT 0"),
            ("bounty", "INTEGER DEFAULT 0"),
            ("crew_id", "INTEGER DEFAULT 0"),
            ("last_work", "INTEGER DEFAULT 0"),
            ("last_daily", "INTEGER DEFAULT 0"),
            ("job_active", "TEXT"),
            ("job_finish_time", "INTEGER DEFAULT 0"),
            ("last_business_collect", "INTEGER DEFAULT 0"),
            ("nama_lengkap", "TEXT DEFAULT 'Warga Anonim'"),
            ("muse", "TEXT DEFAULT 'Tidak Ada'"),
            ("umur", "INTEGER DEFAULT 18"),
            ("tanggal_lahir", "TEXT DEFAULT '01-01-2000'"),
            ("status_sipil", "TEXT DEFAULT 'Lajang'")
        ]
        for col_name, col_type in user_columns:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass  

        await db.execute("""
            CREATE TABLE IF NOT EXISTS families (
                family_id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_name TEXT UNIQUE NOT NULL,
                head_user_id INTEGER NOT NULL,
                family_vault_balance INTEGER NOT NULL DEFAULT 0,
                tax_rate_percent REAL NOT NULL DEFAULT 0,
                is_locked INTEGER NOT NULL DEFAULT 0,
                lock_reason TEXT,
                created_at INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS family_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                loyalty_score INTEGER NOT NULL DEFAULT 100,
                is_active INTEGER NOT NULL DEFAULT 1,
                joined_at INTEGER NOT NULL,
                left_at INTEGER,
                left_reason TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS marriages (
                marriage_id INTEGER PRIMARY KEY AUTOINCREMENT,
                cert_number TEXT UNIQUE NOT NULL,
                user_a_id INTEGER NOT NULL,
                user_b_id INTEGER NOT NULL,
                marriage_type TEXT NOT NULL DEFAULT 'conventional',
                status TEXT NOT NULL DEFAULT 'active',
                married_at INTEGER NOT NULL,
                divorced_at INTEGER,
                divorce_reason TEXT,
                sha256_hash TEXT NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS marriage_proposals (
                proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposer_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                proposal_type TEXT NOT NULL DEFAULT 'conventional',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                responded_at INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS parent_child_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER NOT NULL,
                child_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                registered_at INTEGER NOT NULL,
                registered_by_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                disowned_at INTEGER,
                disowned_reason TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS sibling_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_a_id INTEGER NOT NULL,
                user_b_id INTEGER NOT NULL,
                sibling_type TEXT NOT NULL DEFAULT 'biological',
                registered_at INTEGER NOT NULL,
                registered_by_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS godparent_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                godparent_id INTEGER NOT NULL,
                godchild_id INTEGER NOT NULL,
                registered_at INTEGER NOT NULL,
                registered_by_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                revoked_at INTEGER,
                revoked_reason TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS wills (
                will_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                executed_at INTEGER
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS will_beneficiaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                will_id INTEGER NOT NULL,
                beneficiary_id INTEGER NOT NULL,
                percent REAL NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS inheritance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                will_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                beneficiary_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                executed_at INTEGER NOT NULL
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS lineage_admin_actions (
                action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_type TEXT NOT NULL,
                target_id INTEGER,
                note TEXT,
                requested_by INTEGER NOT NULL,
                approved_by INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                executed_at INTEGER
            )
        """)

        await db.commit()
    except Exception as e:
        logger.warning(f"ensure_all_tables_exist warning: {e}")

async def init_lineage_db():
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)

async def post_init(application):
    await init_lineage_db()
    application.create_task(expire_proposals_loop())

async def expire_proposals_loop():
    while True:
        try:
            now_epoch = int(time.time())
            async with get_db_connection() as db:
                await ensure_all_tables_exist(db)
                await db.execute(
                    "UPDATE marriage_proposals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                    (now_epoch,)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[lineage_bot] expire_proposals_loop error: {e}")
        await asyncio.sleep(PROPOSAL_EXPIRY_CHECK_INTERVAL)

# ==========================================
# GLOBAL ERROR HANDLER
# ==========================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>GANGGUAN TUKAR INFORMASI!</b>\n\n"
            "Sistem arsip sedang mengalami gangguan sinyal. Coba ulangi pilihan Anda sekali lagi. "
            "Jika berlanjut, pastikan identitas Anda sudah terdaftar di Registry.",
            parse_mode="HTML"
        )

# ==========================================
# SHARED HELPERS & NET WORTH CALCULATOR
# ==========================================
async def check_admin_tier(db, user_id: int) -> int:
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4
    try:
        async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else 0
    except Exception:
        return 0

async def user_exists(db, user_id: int) -> bool:
    try:
        async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return (await cursor.fetchone()) is not None
    except Exception:
        return False

async def ensure_user_registered(update: Update, db, user_id: int) -> bool:
    if not await user_exists(db, user_id):
        text = (
            "📋 <b>IDENTITAS TIDAK DITEMUKAN DALAM REKOR KELUARGA!</b>\n\n"
            "Anda belum terdaftar di registry KTP Kota Cosa Nostra. Sistem tidak dapat mencatat aliansi, pernikahan, atau keluarga tanpa dokumen legal.\n\n"
            "👉 <b>Tekan tombol di bawah untuk mendaftar sekarang:</b>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Daftar KTP Sekarang", callback_data="start_registration")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
        return False
    return True

async def get_username(db, user_id: int) -> str:
    try:
        async with db.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else str(user_id)
    except Exception:
        return str(user_id)

async def get_koin(db, user_id: int) -> int:
    try:
        async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception:
        return 0

async def add_koin(db, user_id: int, amount: int):
    await db.execute("UPDATE users SET koin = koin + ? WHERE user_id = ?", (amount, user_id))

async def calculate_net_worth(db, user_id: int) -> tuple:
    try:
        async with db.execute("SELECT koin, bank_balance FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            koin = row[0] if row else 0
            bank = row[1] if row else 0

        vault_share = 0
        async with db.execute(
            """SELECT f.family_vault_balance 
               FROM families f 
               JOIN family_members fm ON f.family_id = fm.family_id 
               WHERE fm.user_id = ? AND fm.relation_type = 'head' AND fm.is_active = 1""",
            (user_id,)
        ) as cursor:
            v_row = await cursor.fetchone()
            if v_row:
                vault_share = v_row[0]

        total_net_worth = koin + bank + vault_share
        return total_net_worth, koin, bank, vault_share
    except Exception:
        return 0, 0, 0, 0

def parse_target_id(context) -> int:
    if not context.args or not context.args[0].lstrip("-").isdigit():
        return None
    return int(context.args[0])

def generate_marriage_certificate(user_a_id: int, user_b_id: int) -> tuple:
    epoch = int(time.time())
    date_str = datetime.now(WIB).strftime("%Y%m%d")
    date_formatted = datetime.now(WIB).strftime("%d %B %Y, %H:%M WIB")
    raw_hash = f"{user_a_id}:{user_b_id}:{epoch}:{random.randint(1000, 9999)}"
    unique_hash = hashlib.sha256(raw_hash.encode()).hexdigest()[:8].upper()
    cert_number = f"CSN-MRG-{date_str}-{epoch}-{unique_hash}"
    full_payload = f"{cert_number}:{user_a_id}:{user_b_id}:{epoch}"
    sha256_verification = hashlib.sha256(full_payload.encode()).hexdigest()
    return cert_number, sha256_verification, date_formatted

async def get_active_marriage(db, user_id: int):
    try:
        await ensure_all_tables_exist(db)
        async with db.execute(
            """SELECT marriage_id, cert_number, user_a_id, user_b_id, married_at, marriage_type
               FROM marriages
               WHERE status = 'active' AND (user_a_id = ? OR user_b_id = ?)""",
            (user_id, user_id)
        ) as cursor:
            return await cursor.fetchone()
    except Exception:
        return None

async def get_active_family_membership(db, user_id: int):
    try:
        await ensure_all_tables_exist(db)
        async with db.execute(
            """SELECT family_id, relation_type, loyalty_score FROM family_members
               WHERE user_id = ? AND is_active = 1""",
            (user_id,)
        ) as cursor:
            return await cursor.fetchone()
    except Exception:
        return None

async def is_ancestor(db, potential_ancestor_id: int, of_user_id: int, max_depth: int = 20) -> bool:
    visited = set()
    frontier = [of_user_id]
    depth = 0
    while frontier and depth < max_depth:
        placeholders = ",".join("?" for _ in frontier)
        try:
            async with db.execute(
                f"""SELECT parent_id, child_id FROM parent_child_relations
                    WHERE is_active = 1 AND child_id IN ({placeholders})""",
                frontier
            ) as cursor:
                rows = await cursor.fetchall()
            
            next_frontier = []
            for parent_id, child_id in rows:
                if parent_id == potential_ancestor_id:
                    return True
                if parent_id not in visited:
                    visited.add(parent_id)
                    next_frontier.append(parent_id)
            frontier = next_frontier
            depth += 1
        except Exception:
            break
    return False

async def is_relative(db, user_a_id: int, user_b_id: int) -> bool:
    if await is_ancestor(db, user_a_id, user_b_id) or await is_ancestor(db, user_b_id, user_a_id):
        return True

    try:
        a_id, b_id = min(user_a_id, user_b_id), max(user_a_id, user_b_id)
        async with db.execute(
            "SELECT 1 FROM sibling_relations WHERE user_a_id = ? AND user_b_id = ? AND is_active = 1",
            (a_id, b_id)
        ) as cursor:
            if await cursor.fetchone():
                return True
    except Exception:
        pass

    try:
        async with db.execute(
            """SELECT 1 FROM godparent_relations 
               WHERE is_active = 1 AND ((godparent_id = ? AND godchild_id = ?) OR (godparent_id = ? AND godchild_id = ?))""",
            (user_a_id, user_b_id, user_b_id, user_a_id)
        ) as cursor:
            if await cursor.fetchone():
                return True
    except Exception:
        pass

    return False

# ==========================================
# CONVERSATION HANDLER: REGISTRATION KTP
# ==========================================
async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message if update.message else update.callback_query.message

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await user_exists(db, user_id):
            text = (
                "📜 <b>IDENTITAS ANDA SUDAH TERDAFTAR!</b>\n\n"
                "KTP Digital Anda sudah tercatat resmi di database. Silakan periksa profil Anda melalui menu utama."
            )
            keyboard = get_back_button()
            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
            else:
                await message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")
            return ConversationHandler.END

    text = (
        "📝 <b>PENDAFTARAN REGISTRY KTP CITIZEN COSA NOSTRA</b>\n\n"
        "Mari lengkapi berkas sipil Anda untuk arsip kota.\n"
        "<b>1. Masukkan NAMA LENGKAP karakter Anda:</b>\n"
        "<i>(Contoh: Don Vitorio Scaletta)</i>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await message.reply_text(text, parse_mode="HTML")
    return REG_NAMA

async def reg_nama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nama_lengkap'] = update.message.text.strip()
    await update.message.reply_text(
        "🎭 <b>NAMA MUSE / AVATAR:</b>\n\n"
        "Masukkan nama Muse / Face Claim (FC) yang digunakan:\n"
        "<i>(Contoh: Character Alpha / Original Concept)</i>",
        parse_mode="HTML"
    )
    return REG_MUSE

async def reg_muse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['muse'] = update.message.text.strip()
    await update.message.reply_text(
        "🎂 <b>USIA OPERASIONAL:</b>\n\n"
        "Masukkan umur karakter Anda (Gunakan format angka):\n"
        "<i>(Contoh: 28)</i>",
        parse_mode="HTML"
    )
    return REG_UMUR

async def reg_umur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Input tidak valid! Usia harus ditulis dengan angka murni. Coba ulang:")
        return REG_UMUR

    context.user_data['umur'] = int(text)
    await update.message.reply_text(
        "📅 <b>TANGGAL LAHIR:</b>\n\n"
        "Masukkan tanggal lahir karakter Anda:\n"
        "<i>(Format: DD-MM-YYYY, Contoh: 15-08-1996)</i>",
        parse_mode="HTML"
    )
    return REG_TGLLAHIR

async def reg_tgl_lahir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    nama = context.user_data.get('nama_lengkap', 'Warga Anonim')
    muse = context.user_data.get('muse', 'Tidak Ada')
    umur = context.user_data.get('umur', 18)
    tgl = update.message.text.strip()
    status_sipil = "Lajang"

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        
        async with db.execute("SELECT gelar_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            gelar = row[0] if row and row[0] else 'G0 (Warga Sipil)'

        await db.execute(
            """INSERT INTO users (user_id, username, koin, bank_balance, vitality, gelar_tier, respect,
                                 nama_lengkap, muse, umur, tanggal_lahir, status_sipil)
               VALUES (?, ?, 10000, 0, 100, ?, 0, ?, ?, ?, ?, ?)""",
            (user_id, username, gelar, nama, muse, umur, tgl, status_sipil)
        )
        await db.commit()

        net_worth, koin, bank, vault = await calculate_net_worth(db, user_id)

    ktp_card = (
        "🪪 <b>KARTU TANDA PENDUKUK (KTP) DIGITAL</b>\n"
        "🏛️ <b>KOTA COSA NOSTRA NETWORK</b>\n\n"
        f"👤 <b>Nama Lengkap :</b> {nama}\n"
        f"🎭 <b>Muse / Avatar :</b> {muse}\n"
        f"🎂 <b>Usia         :</b> {umur} Tahun\n"
        f"📅 <b>Tgl Lahir    :</b> {tgl}\n"
        f"💍 <b>Status Sipil :</b> {status_sipil}\n"
        f"💼 <b>Gelar / Rank :</b> {gelar}\n\n"
        f"💳 <b>ID Citizen   :</b> <code>{user_id}</code>\n"
        f"💵 <b>Modal Tunai   :</b> {koin:,} Koin\n"
        f"💎 <b>Net Worth    :</b> {net_worth:,} Koin\n\n"
        "🎉 <i>Registrasi Berhasil! Dokumen sipil Anda resmi aktif.</i>"
    )

    await update.message.reply_text(ktp_card, reply_markup=get_back_button(), parse_mode="HTML")
    return ConversationHandler.END

async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Pendaftaran dibatalkan.", reply_markup=get_back_button(), parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# CONVERSATION HANDLER: EDIT KTP (KHUSUS ADMIN)
# ==========================================
async def edit_ktp_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text(
                "🚫 <b>OTORITAS TERBATAS:</b> Fitur ini khusus Administrator.",
                reply_markup=get_back_button(),
                parse_mode="HTML"
            )

        if target_id is None:
            return await update.message.reply_text("🛠️ Gunakan format: <code>/edit_ktp [user_id_target]</code>", parse_mode="HTML")

        if not await user_exists(db, target_id):
            return await update.message.reply_text(f"❌ User ID <code>{target_id}</code> tidak terdaftar.", parse_mode="HTML")

    context.user_data['admin_edit_target_id'] = target_id

    keyboard = [
        [InlineKeyboardButton("👤 Nama Lengkap", callback_data="admin_edit_nama_lengkap")],
        [InlineKeyboardButton("🎭 Muse / Avatar", callback_data="admin_edit_muse")],
        [InlineKeyboardButton("🎂 Umur Karakter", callback_data="admin_edit_umur")],
        [InlineKeyboardButton("📅 Tanggal Lahir", callback_data="admin_edit_tanggal_lahir")],
        [InlineKeyboardButton("❌ Batal", callback_data="admin_edit_cancel")]
    ]

    await update.message.reply_text(
        f"🛠️ <b>EDIT KTP WARGA Target:</b> <code>{target_id}</code>\nPilih data yang ingin diubah:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADMIN_EDIT_CHOICE

async def edit_ktp_admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "admin_edit_cancel":
        await query.edit_message_text("❌ Proses revisi KTP dibatalkan.")
        return ConversationHandler.END

    field_map = {
        "admin_edit_nama_lengkap": ("nama_lengkap", "Masukkan **NAMA LENGKAP** baru:"),
        "admin_edit_muse": ("muse", "Masukkan **MUSE / AVATAR** baru:"),
        "admin_edit_umur": ("umur", "Masukkan **USIA** baru (Format Angka):"),
        "admin_edit_tanggal_lahir": ("tanggal_lahir", "Masukkan **TANGGAL LAHIR** baru (DD-MM-YYYY):")
    }

    if data in field_map:
        db_col, prompt = field_map[data]
        context.user_data['admin_edit_col'] = db_col
        await query.edit_message_text(f"✏️ {prompt}", parse_mode="HTML")
        return ADMIN_EDIT_VALUE

    return ADMIN_EDIT_CHOICE

async def edit_ktp_admin_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.user_data.get('admin_edit_target_id')
    target_col = context.user_data.get('admin_edit_col')
    new_value = update.message.text.strip()

    if target_col == "umur":
        if not new_value.isdigit():
            await update.message.reply_text("❌ Input harus berupa angka! Masukkan ulang:")
            return ADMIN_EDIT_VALUE
        new_value = int(new_value)

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        await db.execute(f"UPDATE users SET {target_col} = ? WHERE user_id = ?", (new_value, target_id))
        await db.commit()

        async with db.execute(
            """SELECT nama_lengkap, muse, umur, tanggal_lahir, status_sipil, gelar_tier 
               FROM users WHERE user_id = ?""",
            (target_id,)
        ) as cursor:
            row = await cursor.fetchone()

        nama, muse, umur, tgl, status_sipil, gelar = row
        net_worth, koin, bank, vault = await calculate_net_worth(db, target_id)

    ktp_card = (
        "✅ <b>ARSIP KTP WARGA BERHASIL DIPERBARUI!</b>\n\n"
        "🪪 <b>KARTU TANDA PENDUKUK (KTP) DIGITAL</b>\n"
        f"👤 <b>Nama Lengkap :</b> {nama}\n"
        f"🎭 <b>Muse / Avatar :</b> {muse}\n"
        f"🎂 <b>Usia         :</b> {umur} Tahun\n"
        f"📅 <b>Tgl Lahir    :</b> {tgl}\n"
        f"💍 <b>Status Sipil :</b> {status_sipil}\n"
        f"💼 <b>Gelar / Rank :</b> {gelar}\n\n"
        f"💳 <b>ID Citizen   :</b> <code>{target_id}</code>\n"
        f"💵 <b>Koin Dompet  :</b> {koin:,} Koin\n"
        f"💎 <b>Total Net Worth:</b> {net_worth:,} Koin"
    )

    await update.message.reply_text(ktp_card, reply_markup=get_back_button(), parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# COMMAND VIEW KTP & NET WORTH
# ==========================================
async def cmd_ktp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, target_id):
            text = f"❌ Target ID <code>{target_id}</code> belum memiliki identitas resmi."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, parse_mode="HTML")

        async with db.execute(
            """SELECT nama_lengkap, muse, umur, tanggal_lahir, status_sipil, gelar_tier 
               FROM users WHERE user_id = ?""",
            (target_id,)
        ) as cursor:
            row = await cursor.fetchone()

        nama, muse, umur, tgl, status_sipil, gelar = row
        net_worth, koin, bank, vault = await calculate_net_worth(db, target_id)

        ktp_card = (
            "🪪 <b>KARTU TANDA PENDUKUK (KTP) DIGITAL</b>\n"
            "🏛️ <b>KOTA COSA NOSTRA NETWORK</b>\n\n"
            f"👤 <b>Nama Lengkap :</b> {nama}\n"
            f"🎭 <b>Muse / Avatar :</b> {muse}\n"
            f"🎂 <b>Usia         :</b> {umur} Tahun\n"
            f"📅 <b>Tgl Lahir    :</b> {tgl}\n"
            f"💍 <b>Status Sipil :</b> {status_sipil}\n"
            f"💼 <b>Gelar / Rank :</b> {gelar}\n\n"
            f"💳 <b>ID Citizen   :</b> <code>{target_id}</code>\n"
            f"💵 <b>Koin Dompet  :</b> {koin:,} Koin\n"
            f"💎 <b>Total Net Worth:</b> <b>{net_worth:,} Koin</b>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(ktp_card, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(ktp_card, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_networth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, target_id):
            text = f"❌ Citizen ID <code>{target_id}</code> tidak ditemukan dalam sistem akuntansi."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, parse_mode="HTML")

        target_name = await get_username(db, target_id)
        net_worth, koin, bank, vault = await calculate_net_worth(db, target_id)

        status_ekonomi = "Sipil Biasa"
        if net_worth > 50_000_000:
            status_ekonomi = "👑 Konglomerat Elit"
        elif net_worth > 10_000_000:
            status_ekonomi = "💼 Eksekutif Elit"
        elif net_worth > 1_000_000:
            status_ekonomi = "💵 Pengusaha Sukses"

        text = (
            f"💎 <b>AUDIT FINANCIAL & NET WORTH REPORT</b>\n"
            f"Subjek: <b>@{target_name}</b> (<code>{target_id}</code>)\n\n"
            f"💵 Cash Tunai   : <b>{koin:,} Koin</b>\n"
            f"🏦 Deposit Bank  : <b>{bank:,} Koin</b>\n"
            f"🏛️ Kas Keluarga : <b>{vault:,} Koin</b>\n\n"
            f"🏆 <b>TOTAL NET WORTH : {net_worth:,} Koin</b>\n"
            f"📊 Hirarki Finansial: <b>{status_ekonomi}</b>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# DAILY COMMAND
# ==========================================
async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT last_daily FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            last_daily = row[0] if row and row[0] else 0

        now_epoch = int(time.time())
        cooldown = 86400  
        if now_epoch - last_daily < cooldown:
            remaining = cooldown - (now_epoch - last_daily)
            hours, remainder = divmod(remaining, 3600)
            minutes, seconds = divmod(remainder, 60)
            text = (
                f"⏳ <b>JATAH HARIAN SUDAH DIKLAIM!</b>\n\n"
                f"Tunjangan harian berikutnya baru dapat dicairkan dalam <b>{hours} jam {minutes} menit</b>."
            )
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        daily_reward = 2000
        await add_koin(db, user_id, daily_reward)
        await db.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (now_epoch, user_id))
        await db.commit()

        text = (
            f"🎁 <b>TUNJANGAN HARIAN DICAIRKAN!</b>\n\n"
            f"Kas operasional mendapatkan dana segar <b>+{daily_reward:,} Koin</b>!\n"
            f"Kembalilah besok untuk klaim berikutnya."
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# UTILITY / NEW FEATURES
# ==========================================
async def cmd_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (
        f"💳 <b>INFORMASI CITIZEN ID TELEGRAM</b>\n\n"
        f"ID Anda: <code>{user_id}</code>\n"
        f"Gunakan ID unik ini dalam transaksi diplomatik atau pendaftaran keluarga."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        target_name = await get_username(db, target_id)

        marriage = await get_active_marriage(db, target_id)
        spouse_str = "Belum Ada"
        if marriage:
            spouse_id = marriage[3] if marriage[2] == target_id else marriage[2]
            spouse_name = await get_username(db, spouse_id)
            spouse_str = f"@{spouse_name} (<code>{spouse_id}</code>)"

        async with db.execute(
            "SELECT child_id, relation_type FROM parent_child_relations WHERE parent_id = ? AND is_active = 1",
            (target_id,)
        ) as cursor:
            children = await cursor.fetchall()

        async with db.execute(
            """SELECT user_a_id, user_b_id, sibling_type FROM sibling_relations
               WHERE (user_a_id = ? OR user_b_id = ?) AND is_active = 1""",
            (target_id, target_id)
        ) as cursor:
            siblings = await cursor.fetchall()

        tree_lines = [
            f"🌳 <b>DIAGRAM SILSILAH KELUARGA</b>\n",
            f"👤 <b>{target_name}</b> (<code>{target_id}</code>)",
            f" ┣ 💍 Pasangan Resmi: {spouse_str}"
        ]

        if siblings:
            tree_lines.append(" ┣ 👫 <b>Garis Saudara:</b>")
            for a_id, b_id, s_type in siblings:
                s_id = b_id if a_id == target_id else a_id
                s_name = await get_username(db, s_id)
                tree_lines.append(f" ┃  • @{s_name} (<code>{s_id}</code>) [{s_type}]")

        if children:
            tree_lines.append(" ┗ 👶 <b>Garis Keturunan:</b>")
            for c_id, c_type in children:
                c_name = await get_username(db, c_id)
                label = "Kandung" if c_type == "biological" else "Angkat"
                tree_lines.append(f"    • @{c_name} (<code>{c_id}</code>) [{label}]")
        else:
            tree_lines.append(" ┗ 👶 Keturunan: Belum Tercatat")

        text = "\n".join(tree_lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# MARRIAGE COMMANDS & ENHANCEMENTS
# ==========================================
async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    
    if target_id is None:
        return await update.message.reply_text(
            "💌 <b>PROPOSAL PERSEKUTUAN NIKAH:</b>\n\n"
            "Format pengajuan lamaran:\n"
            "<code>/propose [user_id] [conventional|modern|secret]</code>\n\n"
            "Contoh: <code>/propose 123456789 modern</code>",
            parse_mode="HTML"
        )
        
    if target_id == user_id:
        return await update.message.reply_text("🤔 Mengirim lamaran ke diri sendiri? Cari aliansi dengan pihak lain.")

    m_type = "conventional"
    if len(context.args) > 1 and context.args[1].lower() in ("conventional", "modern", "secret"):
        m_type = context.args[1].lower()

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)

        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, target_id):
            return await update.message.reply_text(
                f"❌ <b>TARGET TIDAK TERDAFTAR!</b>\n\n"
                f"User <code>{target_id}</code> belum tercatat di registry kota. Suruh target mendaftar KTP via <code>/register</code> terlebih dahulu.",
                parse_mode="HTML"
            )

        user_marriage = await get_active_marriage(db, user_id)
        if user_marriage:
            return await update.message.reply_text(
                "💍 <b>STATUS PERNIKAHAN MASIH AKTIF!</b>\n\n"
                "Sistem menolak poligami ganda tanpa perceraian resmi. Putuskan hubungan aktif via <code>/divorce</code> terlebih dahulu.",
                parse_mode="HTML"
            )
            
        target_marriage = await get_active_marriage(db, target_id)
        if target_marriage:
            return await update.message.reply_text(
                f"💔 <b>TARGET SUDAH TERIKAT PERSEKUTUAN!</b>\n\n"
                f"Citizen ID <code>{target_id}</code> telah menjadi pasangan resmi pihak lain. Cari target aliansi yang masih lajang.",
                parse_mode="HTML"
            )

        if await is_relative(db, user_id, target_id):
            return await update.message.reply_text("🚫 <b>PERBEDAAN HUKUM KELUARGA:</b> Sistem menolak pernikahan antar anggota keluarga kandung/relasi terdekat!")

        since_epoch = int(time.time()) - 86400
        count_today = 0
        try:
            async with db.execute(
                "SELECT COUNT(*) FROM marriage_proposals WHERE proposer_id = ? AND created_at > ?",
                (user_id, since_epoch)
            ) as cursor:
                row = await cursor.fetchone()
                count_today = row[0] if row else 0
        except Exception:
            count_today = 0

        if count_today >= MAX_PROPOSALS_PER_DAY:
            return await update.message.reply_text(f"🛑 Batas maksimum lamaran harian ({MAX_PROPOSALS_PER_DAY}x) telah tercapai. Harap tunggu esok hari.")

        has_pending = False
        try:
            async with db.execute(
                "SELECT 1 FROM marriage_proposals WHERE proposer_id = ? AND target_id = ? AND status = 'pending'",
                (user_id, target_id)
            ) as cursor:
                has_pending = (await cursor.fetchone()) is not None
        except Exception:
            has_pending = False

        if has_pending:
            return await update.message.reply_text("⏳ Proposal lamaran Anda masih terikat status PENDING. Menunggu respons target.")

        now_epoch = int(time.time())
        expires_at = now_epoch + PROPOSAL_TTL_SECONDS
        
        await db.execute(
            "INSERT INTO marriage_proposals (proposer_id, target_id, proposal_type, status, created_at, expires_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (user_id, target_id, m_type, now_epoch, expires_at)
        )
        await db.commit()

        target_name = await get_username(db, target_id)
        await update.message.reply_text(
            f"💌 <b>PROPOSAL LAMARAN DITERBITKAN!</b>\n\n"
            f"Ditujukan ke : @{target_name} (<code>{target_id}</code>)\n"
            f"💍 Tipe Akad : <b>{m_type.capitalize()}</b>\n"
            f"⏳ Masa Berlaku: <b>10 Menit</b>\n\n"
            f"Target dapat merespons dengan:\n"
            f"• Terima: <code>/accept_proposal {user_id}</code>\n"
            f"• Tolak: <code>/reject_proposal {user_id}</code>",
            parse_mode="HTML"
        )

async def cmd_accept_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Deteksi apakah dipicu dari CallbackQuery inline button atau Command teks biasa
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        # Format callback_data: accept_prop_{proposer_id}
        parts = query.data.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            proposer_id = int(parts[2])
        else:
            return
    else:
        proposer_id = parse_target_id(context)
        if proposer_id is None:
            return await update.message.reply_text("Format pengesahan: <code>/accept_proposal [proposer_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        async with db.execute(
            """SELECT proposal_id, expires_at, proposal_type FROM marriage_proposals
               WHERE proposer_id = ? AND target_id = ? AND status = 'pending'
               ORDER BY proposal_id DESC LIMIT 1""",
            (proposer_id, user_id)
        ) as cursor:
            proposal = await cursor.fetchone()

        if not proposal:
            text = f"💔 <b>TIDAK ADA LAMARAN PENDING!</b>\n\nTidak ditemukan berkas lamaran aktif dari ID <code>{proposer_id}</code>."
            if update.callback_query:
                return await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, parse_mode="HTML")

        proposal_id, expires_at, m_type = proposal
        if expires_at < now_epoch:
            await db.execute("UPDATE marriage_proposals SET status = 'expired' WHERE proposal_id = ?", (proposal_id,))
            await db.commit()
            text = "⏳ Proposal lamaran telah kadaluarsa (melewati batas waktu 10 menit)."
            if update.callback_query:
                return await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, parse_mode="HTML")

        if await get_active_marriage(db, user_id) or await get_active_marriage(db, proposer_id):
            await db.execute("UPDATE marriage_proposals SET status = 'rejected', responded_at = ? WHERE proposal_id = ?", (now_epoch, proposal_id))
            await db.commit()
            text = "❌ Salah satu pihak telah terikat dalam pernikahan lain."
            if update.callback_query:
                return await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, parse_mode="HTML")

        cert_number, sha_hash, date_formatted = generate_marriage_certificate(proposer_id, user_id)
        
        await db.execute(
            """INSERT INTO marriages (cert_number, user_a_id, user_b_id, marriage_type, status, married_at, sha256_hash)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (cert_number, proposer_id, user_id, m_type, now_epoch, sha_hash)
        )
        await db.execute("UPDATE marriage_proposals SET status = 'accepted', responded_at = ? WHERE proposal_id = ?", (now_epoch, proposal_id))
        
        await db.execute("UPDATE users SET status_sipil = 'Menikah' WHERE user_id IN (?, ?)", (user_id, proposer_id))
        await db.commit()

        proposer_name = await get_username(db, proposer_id)
        my_name = await get_username(db, user_id)
        text = (
            f"💒 <b>PERSEKUTUAN NIKAH RESMI DIESAHKAN!</b>\n\n"
            f"👰🤵 @{proposer_name} (<code>{proposer_id}</code>) ❤️ @{my_name} (<code>{user_id}</code>)\n"
            f"📜 No. Sertifikat: <code>{cert_number}</code>\n"
            f"💍 Tipe Akad : <b>{m_type.capitalize()}</b>\n"
            f"🗓️ Tanggal    : {date_formatted}\n\n"
            f"Status KTP kedua belah pihak resmi diperbarui menjadi Menikah."
        )
        if update.callback_query:
            await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_reject_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        parts = query.data.split("_")
        if len(parts) >= 3 and parts[2].isdigit():
            proposer_id = int(parts[2])
        else:
            return
    else:
        proposer_id = parse_target_id(context)
        if proposer_id is None:
            return await update.message.reply_text("Format penolakan: <code>/reject_proposal [proposer_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        cursor = await db.execute(
            """UPDATE marriage_proposals SET status = 'rejected', responded_at = ?
               WHERE proposer_id = ? AND target_id = ? AND status = 'pending'""",
            (now_epoch, proposer_id, user_id)
        )
        await db.commit()
        
        text = f"💔 Proposal lamaran dari ID <code>{proposer_id}</code> resmi ditolak."
        if update.callback_query:
            await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_proposals_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        async with db.execute(
            """SELECT proposal_id, proposer_id, created_at, expires_at, proposal_type FROM marriage_proposals
               WHERE target_id = ? AND status = 'pending' AND expires_at > ?
               ORDER BY proposal_id DESC""",
            (user_id, now_epoch)
        ) as cursor:
            proposals = await cursor.fetchall()

        if not proposals:
            text = "💌 Tidak ada proposal lamaran pending yang ditujukan kepada Anda saat ini."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        lines = ["💌 <b>DAFTAR PROPOSAL LAMARAN (PENDING)</b>\n"]
        keyboard = []
        for p_id, prop_id, c_at, exp_at, p_type in proposals:
            prop_name = await get_username(db, prop_id)
            rem_sec = exp_at - now_epoch
            lines.append(f"• Dari @{prop_name} (<code>{prop_id}</code>) [{p_type}] — Sisa Waktu: {rem_sec // 60}m {rem_sec % 60}s")
            keyboard.append([
                InlineKeyboardButton(f"✅ Terima @{prop_name}", callback_data=f"accept_prop_{prop_id}"),
                InlineKeyboardButton(f"❌ Tolak @{prop_name}", callback_data=f"reject_prop_{prop_id}")
            ])

        keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")])
        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cmd_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            text = "🤔 Anda sedang dalam status Lajang. Tidak ada ikatan pernikahan untuk diputuskan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE marriages SET status = 'divorced', divorced_at = ?, divorce_reason = 'mutual' WHERE marriage_id = ?",
            (now_epoch, marriage_id)
        )
        
        await db.execute("UPDATE users SET status_sipil = 'Lajang' WHERE user_id IN (?, ?)", (user_a, user_b))
        await db.commit()

        text = (
            f"💔 <b>PERKARA PERCERAIAN DIESAHKAN</b>\n\n"
            f"Pernikahan Anda dengan @{partner_name} (<code>{partner_id}</code>) resmi dibatalkan.\n"
            f"Sertifikat Nikah: <code>{cert_number}</code>\n\n"
            f"<i>Status KTP diperbarui kembali menjadi LAJANG.</i>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_marriage_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            text = (
                "💍 <b>STATUS REKOR: LAJANG</b>\n\n"
                "Anda tidak terikat pernikahan aktif saat ini."
            )
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)
        married_date = datetime.fromtimestamp(married_at, WIB).strftime("%d %B %Y, %H:%M WIB")

        text = (
            f"💍 <b>INFORMASI PERNIKAHAN AKTIF</b>\n\n"
            f"Pasangan: <b>@{partner_name}</b> (<code>{partner_id}</code>)\n"
            f"Sertifikat: <code>{cert_number}</code>\n"
            f"Tipe Akad: <b>{m_type.capitalize()}</b>\n"
            f"Terikat Sejak: {married_date}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💖 Cek Milestone Anniversary", callback_data="action_anniversary")],
            [InlineKeyboardButton("🕊️ Perbarui Janji Setia", callback_data="action_renew_vows")],
            [InlineKeyboardButton("💔 Batalkan Pernikahan (Cerai)", callback_data="action_divorce")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode="HTML")

async def cmd_anniversary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            text = "💍 Anda sedang dalam status Lajang. Tidak ada anniversary pernikahan yang dapat dicatat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)

        now_epoch = int(time.time())
        days_together = (now_epoch - married_at) // 86400

        badge = "🥉 Persekutuan Baru"
        if days_together >= 365:
            badge = "💎 Ikatan Emas (1+ Tahun)"
        elif days_together >= 100:
            badge = "🥇 Ikatan Perak (100+ Hari)"
        elif days_together >= 30:
            badge = "🥈 Ikatan Perunggu (1+ Bulan)"

        text = (
            f"💖 <b>MILESTONE PERSEKUTUAN NIKAH</b>\n\n"
            f"Pasangan: @{partner_name} (<code>{partner_id}</code>)\n"
            f"⏱️ Durasi Bersama: <b>{days_together} Hari</b>\n"
            f"🏆 Badge Hirarki : <b>{badge}</b>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_renew_vows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            text = "💍 Hanya pasangan nikah aktif yang dapat memperbarui janji persekutuan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)
        my_name = await get_username(db, user_id)

        text = (
            f"🕊️ <b>PEMBARUAN JANJI PERSEKUTUAN NIKAH</b>\n\n"
            f"@{my_name} & @{partner_name} menegaskan kembali komitmen dan janji kesetiaan mereka dalam jajaran Cosa Nostra Network! 🍷✨\n\n"
            f"📜 Akta Terikat: <code>{cert_number}</code>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_marriage_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            """SELECT cert_number, user_a_id, user_b_id, married_at, divorced_at, divorce_reason
               FROM marriages
               WHERE status = 'divorced' AND (user_a_id = ? OR user_b_id = ?)
               ORDER BY divorced_at DESC LIMIT 10""",
            (user_id, user_id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = "📜 Rekor masa lalu bersih: Belum ada riwayat perceraian tercatat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        lines = ["📜 <b>RIWAYAT MANTAN PASANGAN (10 Terakhir)</b>\n"]
        for cert, u_a, u_b, m_at, d_at, reason in rows:
            ex_id = u_b if user_id == u_a else u_a
            ex_name = await get_username(db, ex_id)
            d_date = datetime.fromtimestamp(d_at, WIB).strftime("%d %b %Y") if d_at else "-"
            lines.append(f"• Ex: @{ex_name} (<code>{ex_id}</code>)\n  📜 Cert: <code>{cert}</code>\n  🗓️ Cerai: {d_date} ({reason})\n")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# FAMILY COMMANDS
# ==========================================
async def cmd_create_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Gunakan format: <code>/create_family [nama keluarga]</code>", parse_mode="HTML")

    family_name = " ".join(context.args).strip()
    if not (3 <= len(family_name) <= 40):
        return await update.message.reply_text("❌ Nama keluarga harus berkisar 3-40 karakter.")
    if family_name.upper() in BLACKLISTED_FAMILY_NAMES:
        return await update.message.reply_text("🚫 Nama keluarga terlarang/reserved system.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        if await get_active_family_membership(db, user_id):
            return await update.message.reply_text("❌ Anda telah tergabung dalam keluarga aktif.")

        try:
            now_epoch = int(time.time())
            cursor = await db.execute(
                "INSERT INTO families (family_name, head_user_id, created_at) VALUES (?, ?, ?)",
                (family_name, user_id, now_epoch)
            )
            family_id = cursor.lastrowid
            
            await db.execute(
                """INSERT INTO family_members (family_id, user_id, relation_type, loyalty_score, is_active, joined_at)
                   VALUES (?, ?, 'head', 100, 1, ?)""",
                (family_id, user_id, now_epoch)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            return await update.message.reply_text("❌ Nama keluarga tersebut sudah dipatenkan oleh keluarga lain.")

        await update.message.reply_text(f"🏛️ <b>KELUARGA \"{family_name}\" RESMI DIDIRIKAN!</b>", parse_mode="HTML")

async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = "Anda belum menjadi anggota keluarga mana pun saat ini."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        async with db.execute("SELECT family_name, head_user_id, family_vault_balance, tax_rate_percent, is_locked FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()
        
        if not fam:
            text = "❌ Data keluarga tidak ditemukan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            
        family_name, head_id, vault_balance, tax_rate, is_locked = fam

        async with db.execute(
            "SELECT user_id, relation_type, loyalty_score FROM family_members WHERE family_id = ? AND is_active = 1 ORDER BY relation_type",
            (family_id,)
        ) as cursor:
            members = await cursor.fetchall()

        lines = [f"🏛️ <b>KELUARGA {family_name.upper()}</b>{' 🔒' if is_locked else ''}\n"]
        head_name = await get_username(db, head_id)
        lines.append(f"👑 Kepala Keluarga: @{head_name} (<code>{head_id}</code>)")
        lines.append(f"💰 Vault Kas     : <b>{vault_balance:,} Koin</b>")
        lines.append(f"📊 Tarif Pajak   : <b>{tax_rate}%</b>")
        lines.append(f"\n<b>Anggota Aktif ({len(members)}):</b>")
        for m_id, m_rel, m_loyalty in members:
            m_name = await get_username(db, m_id)
            lines.append(f"• <code>{m_id}</code> (@{m_name}) — {m_rel} — Loyalty: {m_loyalty}")

        keyboard = [
            [InlineKeyboardButton("🚪 Keluar Sukarela Dari Keluarga", callback_data="action_leave_family")],
            [InlineKeyboardButton("🗡️ Membelot Dari Keluarga (Betray)", callback_data="action_betray")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _add_child(update: Update, context: ContextTypes.DEFAULT_TYPE, relation_type: str):
    user_id = update.effective_user.id
    child_id = parse_target_id(context)
    if child_id is None:
        cmd = "add_kandung" if relation_type == "biological" else "add_adopt"
        return await update.message.reply_text(f"Format: <code>/{cmd} [user_id]</code>", parse_mode="HTML")
    if child_id == user_id:
        return await update.message.reply_text("🤔 Tidak dapat mendaftarkan diri sendiri sebagai keturunan.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, child_id):
            return await update.message.reply_text(f"❌ Target User {child_id} Belum Terdaftar", parse_mode="HTML")

        if await is_ancestor(db, child_id, user_id):
            return await update.message.reply_text("🚫 Ditolak: Target merupakan garis leluhur Anda!")

        async with db.execute(
            "SELECT 1 FROM parent_child_relations WHERE parent_id = ? AND child_id = ? AND is_active = 1",
            (user_id, child_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi keturunan ini sudah tercatat sebelumnya.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO parent_child_relations (parent_id, child_id, relation_type, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (user_id, child_id, relation_type, now_epoch, user_id)
        )
        await db.commit()

        label = "anak kandung" if relation_type == "biological" else "anak angkat"
        child_name = await get_username(db, child_id)
        await update.message.reply_text(f"👶 @{child_name} (<code>{child_id}</code>) resmi tercatat sebagai <b>{label}</b>!", parse_mode="HTML")

async def cmd_add_kandung(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_child(update, context, "biological")

async def cmd_add_adopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_child(update, context, "adopted")

async def cmd_disown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    child_id = parse_target_id(context)
    if child_id is None:
        return await update.message.reply_text("Format: <code>/disown [user_id]</code>", parse_mode="HTML")

    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Tidak disebutkan"
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        cursor = await db.execute(
            """UPDATE parent_child_relations SET is_active = 0, disowned_at = ?, disowned_reason = ?
               WHERE parent_id = ? AND child_id = ? AND is_active = 1""",
            (now_epoch, reason, user_id, child_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Tidak ditemukan relasi keturunan aktif dengan target ID tersebut.")

        await update.message.reply_text(f"⚔️ Citizen ID <code>{child_id}</code> resmi didisown.", parse_mode="HTML")

async def cmd_leave_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = "Anda belum tergabung dalam keluarga mana pun."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        if relation_type == "head":
            text = "🚫 Kepala Keluarga tidak dapat mengundurkan diri secara langsung."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'voluntary' WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        text = "🚪 Anda resmi keluar dari struktur keluarga secara sukarela."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_betray(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = "Tidak ada keluarga yang dapat dikhianati."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'betrayed', loyalty_score = 0 WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        text = "🗡️ <b>TINDAKAN PENGKHIANATAN TERCATAT.</b>\n\nAnda membelot dari keluarga dengan status <i>betrayed</i>. Skor loyalty direset ke 0!"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_loyalty_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, target_id)
        if not membership:
            text = f"User ID <code>{target_id}</code> tidak memiliki keluarga aktif."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            
        family_id, relation_type, loyalty_score = membership
        text = f"🏆 Loyalitas Citizen ID <code>{target_id}</code>: <b>{loyalty_score}/100</b> ({relation_type})"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_family_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = "Anda belum terikat dalam keluarga mana pun."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        family_id = membership[0]

        async with db.execute(
            """SELECT user_id, relation_type, left_reason, left_at FROM family_members
               WHERE family_id = ? AND is_active = 0 ORDER BY left_at DESC LIMIT 10""",
            (family_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = "📜 Belum ada catatan pengunduran diri / pengeluaran dari keluarga ini."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        lines = ["📜 <b>RIWAYAT LOG KELUARGA (10 Terakhir)</b>\n"]
        for m_id, rel, reason, left_at in rows:
            left_date = datetime.fromtimestamp(left_at, WIB).strftime("%d %b %Y") if left_at else "-"
            m_name = await get_username(db, m_id)
            lines.append(f"• @{m_name} (<code>{m_id}</code>) ({rel}) — {reason} — {left_date}")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_add_sibling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sibling_id = parse_target_id(context)
    if sibling_id is None:
        return await update.message.reply_text("Format: <code>/add_sibling [user_id] [biological|adopted]</code>", parse_mode="HTML")
    if sibling_id == user_id:
        return await update.message.reply_text("🤔 Tidak dapat mengangkat diri sendiri sebagai saudara.")

    sibling_type = "biological"
    if len(context.args) > 1 and context.args[1].lower() in ("biological", "adopted"):
        sibling_type = context.args[1].lower()

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, sibling_id):
            return await update.message.reply_text(f"❌ User ID {sibling_id} tidak terdaftar.", parse_mode="HTML")

        if await is_ancestor(db, sibling_id, user_id) or await is_ancestor(db, user_id, sibling_id):
            return await update.message.reply_text("🚫 Ditolak: Target merupakan garis keturunan / leluhur langsung Anda.")

        a_id, b_id = min(user_id, sibling_id), max(user_id, sibling_id)
        async with db.execute(
            "SELECT 1 FROM sibling_relations WHERE user_a_id = ? AND user_b_id = ? AND is_active = 1",
            (a_id, b_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi saudara ini sudah terdaftar.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO sibling_relations (user_a_id, user_b_id, sibling_type, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (a_id, b_id, sibling_type, now_epoch, user_id)
        )
        await db.commit()

        label = "saudara kandung" if sibling_type == "biological" else "saudara angkat"
        sibling_name = await get_username(db, sibling_id)
        await update.message.reply_text(f"👫 @{sibling_name} (<code>{sibling_id}</code>) resmi terdaftar sebagai <b>{label}</b> Anda!", parse_mode="HTML")

async def cmd_siblings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            """SELECT user_a_id, user_b_id, sibling_type FROM sibling_relations
               WHERE (user_a_id = ? OR user_b_id = ?) AND is_active = 1""",
            (target_id, target_id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = f"👫 Citizen ID <code>{target_id}</code> belum memiliki saudara yang tercatat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        lines = [f"👫 <b>GARIS SAUDARA UNTUK ID <code>{target_id}</code></b>\n"]
        for a_id, b_id, s_type in rows:
            other_id = b_id if a_id == target_id else a_id
            other_name = await get_username(db, other_id)
            label = "kandung" if s_type == "biological" else "angkat"
            lines.append(f"• @{other_name} (<code>{other_id}</code>) ({label})")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_godparent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    godparent_id = parse_target_id(context)
    if godparent_id is None:
        return await update.message.reply_text("Format: <code>/godparent [user_id]</code>", parse_mode="HTML")
    if godparent_id == user_id:
        return await update.message.reply_text("❌ Tidak dapat menunjuk diri sendiri sebagai Godparent.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, godparent_id):
            return await update.message.reply_text(f"❌ User ID {godparent_id} belum terdaftar.", parse_mode="HTML")

        async with db.execute(
            "SELECT 1 FROM godparent_relations WHERE godparent_id = ? AND godchild_id = ? AND is_active = 1",
            (godparent_id, user_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi Godparent ini sudah aktif.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO godparent_relations (godparent_id, godchild_id, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, 1)""",
            (godparent_id, user_id, now_epoch, user_id)
        )
        await db.commit()

        gp_name = await get_username(db, godparent_id)
        await update.message.reply_text(
            f"🕯️ @{gp_name} (<code>{godparent_id}</code>) resmi diangkat sebagai <b>Godparent</b> Anda!",
            parse_mode="HTML"
        )

async def cmd_revoke_godparent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    godparent_id = parse_target_id(context)
    if godparent_id is None:
        return await update.message.reply_text("Format: <code>/revoke_godparent [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        cursor = await db.execute(
            """UPDATE godparent_relations SET is_active = 0, revoked_at = ?, revoked_reason = 'voluntary'
               WHERE godparent_id = ? AND godchild_id = ? AND is_active = 1""",
            (now_epoch, godparent_id, user_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Tidak ditemukan relasi Godparent aktif dengan target tersebut.")
        await update.message.reply_text(f"🕯️ Status Godparent <code>{godparent_id}</code> telah dicabut.", parse_mode="HTML")

async def cmd_my_godchildren(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            "SELECT godchild_id FROM godparent_relations WHERE godparent_id = ? AND is_active = 1",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = "🕯️ Anda belum ditunjuk sebagai Godparent oleh Citizen mana pun."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        lines = ["🕯️ <b>DAFTAR GODCHILDREN ANDA</b>\n"]
        for r in rows:
            gc_name = await get_username(db, r[0])
            lines.append(f"• @{gc_name} (<code>{r[0]}</code>)")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_in_laws(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            text = "Anda belum menikah. Relasi mertua/in-laws hanya berlaku bagi yang memiliki pasangan resmi."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        spouse_id = user_b if user_id == user_a else user_a
        spouse_name = await get_username(db, spouse_id)

        spouse_family = await get_active_family_membership(db, spouse_id)
        own_family = await get_active_family_membership(db, user_id)

        lines = [f"👪 <b>RELASI IN-LAWS (KELUARGA PASANGAN @{spouse_name})</b>\n"]

        if own_family and spouse_family and own_family[0] == spouse_family[0]:
            lines.append("Anda dan pasangan berada dalam keluarga besar yang sama!")
        elif not spouse_family:
            lines.append("Pasangan Anda belum bergabung dengan keluarga mana pun.")
        else:
            spouse_family_id = spouse_family[0]
            async with db.execute("SELECT family_name, head_user_id FROM families WHERE family_id = ?", (spouse_family_id,)) as cursor:
                fam = await cursor.fetchone()
            family_name, head_id = fam
            head_name = await get_username(db, head_id)
            lines.append(f"Keluarga: <b>{family_name}</b>")
            lines.append(f"Mertua/Kepala Keluarga: <b>@{head_name}</b> (<code>{head_id}</code>)")

            async with db.execute(
                "SELECT user_id, relation_type FROM family_members WHERE family_id = ? AND is_active = 1 AND user_id != ?",
                (spouse_family_id, spouse_id)
            ) as cursor:
                members = await cursor.fetchall()
            if members:
                lines.append("\n<b>Anggota Ipar/Lainnya:</b>")
                for m_id, m_rel in members:
                    m_name = await get_username(db, m_id)
                    lines.append(f"• @{m_name} (<code>{m_id}</code>) ({m_rel})")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# ADVANCED FAMILY MANAGEMENT
# ==========================================
async def cmd_deposit_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/deposit_vault [jumlah_koin]</code>", parse_mode="HTML")

    amount = int(context.args[0])
    if amount <= 0:
        return await update.message.reply_text("❌ Jumlah deposit koin harus bernilai positif.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda belum tergabung dalam keluarga mana pun.")

        family_id = membership[0]

        async with db.execute("SELECT is_locked FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()
            if fam and fam[0] == 1:
                return await update.message.reply_text("🔒 Vault keluarga dikunci oleh Administrator.")

        user_koin = await get_koin(db, user_id)
        if user_koin < amount:
            return await update.message.reply_text(f"❌ Saldo dompet Anda tidak mencukupi! (Saldo saat ini: <b>{user_koin:,} Koin</b>).", parse_mode="HTML")

        await add_koin(db, user_id, -amount)
        await db.execute("UPDATE families SET family_vault_balance = family_vault_balance + ? WHERE family_id = ?", (amount, family_id))
        await db.commit()

        await update.message.reply_text(f"💰 Berhasil mendepositkan <b>{amount:,} Koin</b> ke Vault Kas Keluarga!", parse_mode="HTML")

async def cmd_withdraw_vault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/withdraw_vault [jumlah_koin]</code>", parse_mode="HTML")

    amount = int(context.args[0])
    if amount <= 0:
        return await update.message.reply_text("❌ Penarikan dana harus bernilai positif.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda belum tergabung dalam keluarga mana pun.")

        family_id, relation_type, _ = membership
        if relation_type != "head":
            return await update.message.reply_text("🚫 Otoritas Terbatas: Penarikan dana kas keluarga hanya dapat dilakukan oleh Kepala Keluarga.", parse_mode="HTML")

        async with db.execute("SELECT family_vault_balance, is_locked FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()

        if not fam:
            return await update.message.reply_text("❌ Data keluarga tidak ditemukan.")

        vault_balance, is_locked = fam
        if is_locked == 1:
            return await update.message.reply_text("🔒 Vault keluarga sedang dikunci oleh Administrator.")

        if vault_balance < amount:
            return await update.message.reply_text(f"❌ Kas Vault Keluarga tidak mencukupi! Saldo vault saat ini: <b>{vault_balance:,} Koin</b>.", parse_mode="HTML")

        await db.execute("UPDATE families SET family_vault_balance = family_vault_balance - ? WHERE family_id = ?", (amount, family_id))
        await add_koin(db, user_id, amount)
        await db.commit()

        await update.message.reply_text(f"💸 Berhasil menarik <b>{amount:,} Koin</b> dari Vault Keluarga ke rekening pribadi!", parse_mode="HTML")

async def cmd_set_family_tax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].replace('.', '', 1).isdigit():
        return await update.message.reply_text("Format: <code>/set_family_tax [0-100]</code>", parse_mode="HTML")

    tax_rate = float(context.args[0])
    if not (0.0 <= tax_rate <= 100.0):
        return await update.message.reply_text("❌ Persentase tarif pajak harus berada di kisaran 0% hingga 100%.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership or membership[1] != "head":
            return await update.message.reply_text("🚫 Pengaturan pajak keluarga hanya dapat ditetapkan oleh Kepala Keluarga.", parse_mode="HTML")

        family_id = membership[0]
        await db.execute("UPDATE families SET tax_rate_percent = ? WHERE family_id = ?", (tax_rate, family_id))
        await db.commit()

        await update.message.reply_text(f"📊 Tarif pajak operasional keluarga diperbarui menjadi <b>{tax_rate}%</b>.", parse_mode="HTML")

async def cmd_transfer_head(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text("Format: <code>/transfer_head [user_id_penerus]</code>", parse_mode="HTML")

    if target_id == user_id:
        return await update.message.reply_text("🤔 Anda sudah memegang kepemimpinan tertinggi keluarga saat ini.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership or membership[1] != "head":
            return await update.message.reply_text("🚫 Hanya Kepala Keluarga yang dapat mengalihkan kepemimpinan.", parse_mode="HTML")

        family_id = membership[0]

        target_membership = await get_active_family_membership(db, target_id)
        if not target_membership or target_membership[0] != family_id:
            return await update.message.reply_text("❌ Target penerus bukan anggota aktif dari keluarga Anda.")

        await db.execute("UPDATE families SET head_user_id = ? WHERE family_id = ?", (target_id, family_id))
        await db.execute("UPDATE family_members SET relation_type = 'member' WHERE family_id = ? AND user_id = ?", (family_id, user_id))
        await db.execute("UPDATE family_members SET relation_type = 'head' WHERE family_id = ? AND user_id = ?", (family_id, target_id))
        await db.commit()

        target_name = await get_username(db, target_id)
        await update.message.reply_text(f"👑 <b>TAKHTA KEPEMIMPINAN DIALIHKAN</b> kepada @{target_name}!", parse_mode="HTML")

async def cmd_kick_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text("Format: <code>/kick_member [user_id] [alasan]</code>", parse_mode="HTML")

    if target_id == user_id:
        return await update.message.reply_text("Gunakan <code>/leave_family</code> jika ingin mengundurkan diri.")

    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Keputusan Kepala Keluarga"

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership or membership[1] != "head":
            return await update.message.reply_text("🚫 Hanya Kepala Keluarga yang berhak mengeluarkan anggota.", parse_mode="HTML")

        family_id = membership[0]
        target_membership = await get_active_family_membership(db, target_id)
        if not target_membership or target_membership[0] != family_id:
            return await update.message.reply_text("❌ Target bukan anggota aktif di keluarga Anda.")

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = ? WHERE family_id = ? AND user_id = ?",
            (now_epoch, f"kicked: {reason}", family_id, target_id)
        )
        await db.commit()

        target_name = await get_username(db, target_id)
        await update.message.reply_text(f"👞 @{target_name} (<code>{target_id}</code>) telah dikeluarkan dari keluarga.", parse_mode="HTML")

# ==========================================
# INHERITANCE / WILL COMMANDS
# ==========================================
async def cmd_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text(
            "📜 <b>FORMAT DOKUMEN WASIAT:</b>\n\n"
            "<code>/will [user_id]:[persen] [user_id]:[persen] ...</code>\n\n"
            "Contoh:\n<code>/will 123456789:50 987654321:30</code>",
            parse_mode="HTML"
        )

    beneficiaries = []
    total_percent = 0.0
    for arg in context.args:
        if ":" not in arg:
            return await update.message.reply_text(f"❌ Format penulisan salah: <code>{arg}</code>.", parse_mode="HTML")
        b_id_str, pct_str = arg.split(":", 1)
        if not b_id_str.isdigit():
            return await update.message.reply_text(f"❌ Target ID harus berupa angka: <code>{arg}</code>", parse_mode="HTML")
        try:
            pct = float(pct_str)
        except ValueError:
            return await update.message.reply_text(f"❌ Persentase harus angka: <code>{arg}</code>", parse_mode="HTML")
        b_id = int(b_id_str)
        if b_id == user_id:
            return await update.message.reply_text("🤔 Tidak dapat membagikan hak waris kepada diri sendiri.")
        if pct <= 0:
            return await update.message.reply_text("❌ Nilai persentase warisan harus positif.")
        beneficiaries.append((b_id, pct))
        total_percent += pct

    if total_percent > 100:
        return await update.message.reply_text(f"❌ Akumulasi alokasi ({total_percent:.1f}%) melebihi kuota 100%!", parse_mode="HTML")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        for b_id, _ in beneficiaries:
            if not await user_exists(db, b_id):
                return await update.message.reply_text(f"❌ Penerima waris <code>{b_id}</code> belum terdaftar di registry.", parse_mode="HTML")

        now_epoch = int(time.time())
        async with db.execute("SELECT will_id FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            existing = await cursor.fetchone()

        if existing:
            will_id = existing[0]
            await db.execute("DELETE FROM will_beneficiaries WHERE will_id = ?", (will_id,))
            await db.execute("UPDATE wills SET status = 'active', updated_at = ? WHERE will_id = ?", (now_epoch, will_id))
        else:
            cursor = await db.execute(
                "INSERT INTO wills (owner_id, status, created_at, updated_at) VALUES (?, 'active', ?, ?)",
                (user_id, now_epoch, now_epoch)
            )
            will_id = cursor.lastrowid

        for b_id, pct in beneficiaries:
            await db.execute(
                "INSERT INTO will_beneficiaries (will_id, beneficiary_id, percent) VALUES (?, ?, ?)",
                (will_id, b_id, pct)
            )
        await db.commit()

        lines = ["📜 <b>SURAT WASIAT WARIS RESMI DIPERBARUI ✅</b>\n"]
        for b_id, pct in beneficiaries:
            b_name = await get_username(db, b_id)
            lines.append(f"• @{b_name} (<code>{b_id}</code>) — {pct}%")
        
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_appoint_heir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    heir_id = parse_target_id(context)
    if heir_id is None:
        return await update.message.reply_text("Format: <code>/appoint_heir [user_id]</code>", parse_mode="HTML")
    if heir_id == user_id:
        return await update.message.reply_text("❌ Tidak dapat menunjuk diri sendiri sebagai ahli waris tunggal.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return
        if not await user_exists(db, heir_id):
            return await update.message.reply_text(f"❌ User ID {heir_id} belum terdaftar.", parse_mode="HTML")

    context.args = [f"{heir_id}:100"]
    await cmd_will(update, context)

async def cmd_will_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status, updated_at, executed_at FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            text = "📜 Belum ada dokumen wasiat yang diterbitkan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        will_id, status, updated_at, executed_at = will
        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = "📜 Dokumen wasiat Anda masih kosong."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        status_label = "EXECUTED" if status == "executed" else "ACTIVE"
        lines = [f"📜 <b>DOKUMEN WASIAT — Status: {status_label}</b>\n"]
        for b_id, pct in rows:
            b_name = await get_username(db, b_id)
            lines.append(f"• @{b_name} (<code>{b_id}</code>) — {pct}%")
            
        keyboard = []
        if executed_at:
            exec_date = datetime.fromtimestamp(executed_at, WIB).strftime("%d %B %Y, %H:%M WIB")
            lines.append(f"\n✅ <b>TELAH DIEKSEKUSI PADA:</b> {exec_date}")
        else:
            keyboard.append([InlineKeyboardButton("⚰️ Eksekusi Pensiun & Bagikan Warisan", callback_data="action_retire")])
            keyboard.append([InlineKeyboardButton("🗑️ Batalkan Dokumen Wasiat", callback_data="action_cancel_will")])

        keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")])

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cmd_cancel_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()

        if not will:
            text = "📜 Anda tidak memiliki dokumen wasiat aktif."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        will_id, status = will
        if status == "executed":
            text = "❌ Dokumen wasiat yang telah dieksekusi tidak dapat dibatalkan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        await db.execute("DELETE FROM will_beneficiaries WHERE will_id = ?", (will_id,))
        await db.execute("DELETE FROM wills WHERE will_id = ?", (will_id,))
        await db.commit()

        text = "🗑️ Dokumen wasiat resmi dibatalkan dan dimusnahkan dari arsip."
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

async def cmd_retire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            text = "❌ Anda belum menerbitkan dokumen wasiat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        will_id, status = will
        if status == "executed":
            text = "❌ Dokumen wasiat ini telah dieksekusi sebelumnya."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            beneficiaries = await cursor.fetchall()
        if not beneficiaries:
            text = "❌ Alokasi dokumen wasiat Anda masih kosong."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        total_koin = await get_koin(db, user_id)
        if total_koin <= 0:
            text = "❌ Saldo likuid Anda 0 Koin. Tidak ada aset likuid yang dapat diwariskan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

        now_epoch = int(time.time())
        distributed_lines = []
        remaining = total_koin
        total_distributed = 0
        
        for b_id, pct in beneficiaries:
            amount = int(total_koin * (pct / 100))
            if amount <= 0:
                continue
            await add_koin(db, b_id, amount)
            remaining -= amount
            total_distributed += amount
            b_name = await get_username(db, b_id)
            await db.execute(
                "INSERT INTO inheritance_log (will_id, owner_id, beneficiary_id, amount, executed_at) VALUES (?, ?, ?, ?, ?)",
                (will_id, user_id, b_id, amount, now_epoch)
            )
            distributed_lines.append(f"• @{b_name} (<code>{b_id}</code>) ← <b>{amount:,} Koin</b> ({pct:.0f}%)")

        await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (remaining, user_id))
        await db.execute("UPDATE wills SET status = 'executed', executed_at = ? WHERE owner_id = ?", (now_epoch, user_id))
        await db.commit()

        text = (
            "⚰️ <b>PROSES EKSEKUSI WASIAT (PENSIUN) BERHASIL!</b>\n\n"
            + "\n".join(distributed_lines)
            + f"\n\n💰 Total Aset Diwariskan: <b>{total_distributed:,} Koin</b>"
            + f"\n💾 Sisa Saldo Tersimpan : <b>{remaining:,} Koin</b>"
            + "\n\n✅ <i>Status pensiun resmi disahkan.</i>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button(), parse_mode="HTML")

# ==========================================
# ADMIN COMMANDS
# ==========================================
async def cmd_lineage_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.", parse_mode="HTML")

        text = (
            f"🛠️ <b>LINEAGE ADMIN PANEL</b>\n\n"
            f"Level Otoritas Anda: <b>Tier {tier}</b>\n\n"
            f"<b>Fitur Admin:</b>\n"
            f"• <code>/edit_ktp [user_id]</code>\n"
            f"• <code>/audit_kekayaan [user_id]</code>\n"
            f"• <code>/set_koin_admin [user_id] [koin]</code>\n"
            f"• <code>/rename_family [family_id] [nama_baru]</code>\n"
            f"• <code>/lock_family [family_id] [alasan]</code>\n"
            f"• <code>/unlock_family [family_id]</code>\n"
            f"• <code>/force_divorce [user_id]</code>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_audit_kekayaan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 1+.")

        if not context.args or not context.args[0].isdigit():
            return await update.message.reply_text("Format: <code>/audit_kekayaan [target_id]</code>", parse_mode="HTML")

        target_id = int(context.args[0])
        if not await user_exists(db, target_id):
            return await update.message.reply_text(f"❌ User ID <code>{target_id}</code> tidak ditemukan.", parse_mode="HTML")

        target_name = await get_username(db, target_id)
        net_worth, koin, bank, vault = await calculate_net_worth(db, target_id)

        text = (
            f"🔍 <b>FINANCIAL AUDIT REPORT (ADMIN)</b>\n\n"
            f"Target: <code>{target_id}</code> (@{target_name})\n\n"
            f"💵 Saldo Tunai: <b>{koin:,} Koin</b>\n"
            f"🏦 Saldo Bank: <b>{bank:,} Koin</b>\n"
            f"🏛️ Kas Vault : <b>{vault:,} Koin</b>\n\n"
            f"💎 <b>TOTAL NET WORTH: {net_worth:,} Koin</b>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_set_koin_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        return await update.message.reply_text("Format: <code>/set_koin_admin [target_id] [jumlah_koin]</code>", parse_mode="HTML")

    target_id = int(context.args[0])
    amount = int(context.args[1])

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        if not await user_exists(db, target_id):
            return await update.message.reply_text(f"❌ User ID <code>{target_id}</code> tidak terdaftar.", parse_mode="HTML")

        await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (amount, target_id))
        await db.commit()

        await update.message.reply_text(f"✅ <b>ADMIN CONTROL:</b> Saldo Koin <code>{target_id}</code> diset menjadi <b>{amount:,} Koin</b>.", parse_mode="HTML")

async def cmd_rename_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2 or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/rename_family [family_id] [nama_keluarga_baru]</code>", parse_mode="HTML")

    family_id = int(context.args[0])
    new_family_name = " ".join(context.args[1:]).strip()

    if not (3 <= len(new_family_name) <= 40):
        return await update.message.reply_text("❌ Nama keluarga baru harus 3-40 karakter.")

    if new_family_name.upper() in BLACKLISTED_FAMILY_NAMES:
        return await update.message.reply_text("🚫 Nama keluarga ini terlarang/reserved system.")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        async with db.execute("SELECT family_name FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()

        if not fam:
            return await update.message.reply_text(f"❌ Family ID <code>{family_id}</code> tidak ditemukan.", parse_mode="HTML")

        old_name = fam[0]

        try:
            await db.execute("UPDATE families SET family_name = ? WHERE family_id = ?", (new_family_name, family_id))
            await db.commit()
        except aiosqlite.IntegrityError:
            return await update.message.reply_text("❌ Nama keluarga tersebut sudah digunakan oleh keluarga lain.")

        await update.message.reply_text(f"✅ Nama keluarga diubah dari <s>{old_name}</s> menjadi <b>{new_family_name}</b>", parse_mode="HTML")

async def cmd_lock_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/lock_family [family_id] [alasan]</code>", parse_mode="HTML")
    family_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Investigasi Admin"

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")
        cursor = await db.execute("UPDATE families SET is_locked = 1, lock_reason = ? WHERE family_id = ?", (reason, family_id))
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Family ID tidak ditemukan.")
        await update.message.reply_text(f"🔒 Akses Keluarga <code>{family_id}</code> dikunci.", parse_mode="HTML")

async def cmd_unlock_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/unlock_family [family_id]</code>", parse_mode="HTML")
    family_id = int(context.args[0])

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")
        cursor = await db.execute("UPDATE families SET is_locked = 0, lock_reason = NULL WHERE family_id = ?", (family_id,))
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Family ID tidak ditemukan.")
        await update.message.reply_text(f"🔓 Akses Keluarga <code>{family_id}</code> dibuka kembali.", parse_mode="HTML")

async def cmd_force_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text("Format: <code>/force_divorce [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        marriage = await get_active_marriage(db, target_id)
        if not marriage:
            return await update.message.reply_text("❌ Target tidak sedang terikat pernikahan aktif.")

        marriage_id = marriage[0]
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE marriages SET status = 'divorced', divorced_at = ?, divorce_reason = 'force_admin' WHERE marriage_id = ?",
            (now_epoch, marriage_id)
        )
        await db.commit()
        await update.message.reply_text(f"⚖️ <b>FORCE DIVORCE</b> dieksekusi oleh Administrator.", parse_mode="HTML")

# ==========================================
# SYSTEM SUB-MENU INTERAKTIF (INLINE KEYBOARD)
# ==========================================
def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📝 Identitas & KTP", callback_data="menu_utilitas"),
            InlineKeyboardButton("💍 Pernikahan & Pasangan", callback_data="menu_nikah")
        ],
        [
            InlineKeyboardButton("🏛️ Dinasti & Silsilah Keluarga", callback_data="menu_keluarga"),
            InlineKeyboardButton("⚰️ Wasiat & Pembagian Warisan", callback_data="menu_warisan")
        ],
        [
            InlineKeyboardButton("🛠️ Panel Pengelola System", callback_data="menu_admin")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "✨ <b>Selamat Datang di Portal Kependudukan & Silsilah Cosa Nostra!</b>\n\n"
        "Sistem ini membantu Anda mengelola kartu identitas digital, hubungan pernikahan, silsilah keturunan keluarga, serta pembagian warisan secara otomatis dan rapi.\n\n"
        "💡 <b>Panduan Singkat untuk Pemula:</b>\n"
        "• Jika Anda pengguna baru, tekan tombol <b>\"📝 Daftar KTP Sekarang\"</b> untuk memulai.\n"
        "• Seluruh fitur dapat dijalankan secara instan dengan menekan tombol navigasi di bawah ini tanpa perlu mengetik kode/perintah manual.\n\n"
        "Silakan pilih layanan yang Anda butuhkan:"
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

    if data == "menu_main":
        await start(update, context)

    elif data == "start_registration":
        await reg_start(update, context)

    elif data == "menu_utilitas":
        text = (
            "📝 <b>LAYANAN IDENTITAS & KTP CITIZEN</b>\n\n"
            "Menu ini mengelola data kependudukan dan keuangan dasar Anda. Pilih tombol di bawah untuk menjalankan fungsi:"
        )
        keyboard = [
            [InlineKeyboardButton("📝 Daftar KTP Baru", callback_data="start_registration")],
            [InlineKeyboardButton("🪪 Lihat Kartu Identitas (KTP)", callback_data="action_ktp")],
            [InlineKeyboardButton("💎 Audit Kekayaan Bersih (Net Worth)", callback_data="action_networth")],
            [InlineKeyboardButton("🎁 Klaim Tunjangan Kas Harian", callback_data="action_daily")],
            [InlineKeyboardButton("💳 Cek ID Unik Telegram Anda", callback_data="action_my_id")],
            [InlineKeyboardButton("🌳 Tampilkan Bagan Silsilah Keluarga", callback_data="action_tree")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_nikah":
        text = (
            "💍 <b>LAYANAN PERNIKAHAN & HUBUNGAN PASANGAN</b>\n\n"
            "Atur hubungan persekutuan nikah resmi, periksa milestone anniversary, atau kelola lamaran masuk:"
        )
        keyboard = [
            [InlineKeyboardButton("💍 Cek Status Pernikahan Aktif", callback_data="action_marriage_status")],
            [InlineKeyboardButton("💌 Lihat Lamaran Nikah yang Masuk", callback_data="action_proposals_list")],
            [InlineKeyboardButton("💖 Cek Durasi & Badge Anniversary", callback_data="action_anniversary")],
            [InlineKeyboardButton("🕊️ Perbarui Janji Setia Pasangan", callback_data="action_renew_vows")],
            [InlineKeyboardButton("💔 Batalkan Pernikahan (Cerai)", callback_data="action_divorce")],
            [InlineKeyboardButton("📜 Lihat Riwayat Mantan Pasangan", callback_data="action_marriage_history")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_keluarga":
        text = (
            "🏛️ <b>LAYANAN DINASTI & STRUKTUR KELUARGA</b>\n\n"
            "Kelola keanggotaan keluarga besar, hubungkan garis keturunan saudara, serta periksa kondisi kas keluarga:"
        )
        keyboard = [
            [InlineKeyboardButton("🏛️ Lihat Anggota & Kas Keluarga", callback_data="action_family")],
            [InlineKeyboardButton("🏆 Periksa Tingkat Loyalitas Anda", callback_data="action_loyalty_check")],
            [InlineKeyboardButton("📜 Lihat Catatan Keluar/Masuk Anggota", callback_data="action_family_history")],
            [InlineKeyboardButton("👫 Cek Garis Saudara Anda", callback_data="action_siblings")],
            [InlineKeyboardButton("🕯️ Cek Daftar Anak Baptis (Godchildren)", callback_data="action_my_godchildren")],
            [InlineKeyboardButton("👪 Lihat Struktur Keluarga Pasangan (Mertua)", callback_data="action_in_laws")],
            [InlineKeyboardButton("🚪 Keluar Dari Keluarga", callback_data="action_leave_family")],
            [InlineKeyboardButton("🗡️ Membelot Dari Keluarga (Betray)", callback_data="action_betray")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_warisan":
        text = (
            "⚰️ <b>LAYANAN WASIAT & PEMBAGIAN WARISAN</b>\n\n"
            "Atur pembagian harta kekayaan Anda kepada ahli waris yang ditunjuk agar aman di masa mendatang:"
        )
        keyboard = [
            [InlineKeyboardButton("📜 Cek Isi Dokumen Wasiat Aktif", callback_data="action_will_status")],
            [InlineKeyboardButton("⚰️ Eksekusi Pensiun & Bagikan Warisan", callback_data="action_retire")],
            [InlineKeyboardButton("🗑️ Batalkan Dokumen Wasiat", callback_data="action_cancel_will")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_admin":
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            await ensure_all_tables_exist(db)
            tier = await check_admin_tier(db, user_id)
            if tier == 0:
                return await query.edit_message_text(
                    "🚫 <b>AKSES DITOLAK:</b> Fitur ini khusus untuk Administrator/Pengelola Sistem.",
                    reply_markup=get_back_button(),
                    parse_mode="HTML"
                )

            text = (
                f"🛠️ <b>PANEL PENGELOLA SISTEM (ADMINISTRATOR)</b>\n\n"
                f"Tingkat Otoritas Anda: <b>Tier {tier}</b>\n\n"
                f"Menu ini berisi alat kontrol khusus untuk pengelolaan database kependudukan, audit finansial warga, dan penyelesaian sengketa keluarga."
            )
            await query.edit_message_text(text, reply_markup=get_back_button(), parse_mode="HTML")

    # ==========================================
    # HANDLER ACTION INTERAKTIF VIA BUTTON
    # ==========================================
    elif data.startswith("accept_prop_"):
        await cmd_accept_proposal(update, context)
    elif data.startswith("reject_prop_"):
        await cmd_reject_proposal(update, context)

    elif data.startswith("action_"):
        action = data.replace("action_", "")
        fake_update = Update(update.update_id, message=query.message)
        fake_update._effective_user = update.effective_user
        context.args = []

        if action == "ktp":
            await cmd_ktp(fake_update, context)
        elif action == "networth":
            await cmd_networth(fake_update, context)
        elif action == "daily":
            await cmd_daily(fake_update, context)
        elif action == "my_id":
            await cmd_my_id(fake_update, context)
        elif action == "tree":
            await cmd_tree(fake_update, context)
        elif action == "marriage_status":
            await cmd_marriage_status(fake_update, context)
        elif action == "proposals_list":
            await cmd_proposals_list(fake_update, context)
        elif action == "anniversary":
            await cmd_anniversary(fake_update, context)
        elif action == "renew_vows":
            await cmd_renew_vows(fake_update, context)
        elif action == "divorce":
            await cmd_divorce(fake_update, context)
        elif action == "marriage_history":
            await cmd_marriage_history(fake_update, context)
        elif action == "family":
            await cmd_family(fake_update, context)
        elif action == "loyalty_check":
            await cmd_loyalty_check(fake_update, context)
        elif action == "family_history":
            await cmd_family_history(fake_update, context)
        elif action == "siblings":
            await cmd_siblings(fake_update, context)
        elif action == "my_godchildren":
            await cmd_my_godchildren(fake_update, context)
        elif action == "in_laws":
            await cmd_in_laws(fake_update, context)
        elif action == "leave_family":
            await cmd_leave_family(fake_update, context)
        elif action == "betray":
            await cmd_betray(fake_update, context)
        elif action == "will_status":
            await cmd_will_status(fake_update, context)
        elif action == "cancel_will":
            await cmd_cancel_will(fake_update, context)
        elif action == "retire":
            await cmd_retire(fake_update, context)

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    # Conversation Handler Pendaftaran KTP
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", reg_start),
            CallbackQueryHandler(reg_start, pattern="^start_registration$")
        ],
        states={
            REG_NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_nama)],
            REG_MUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_muse)],
            REG_UMUR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_umur)],
            REG_TGLLAHIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_tgl_lahir)]
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)]
    )
    app.add_handler(reg_conv)

    # Conversation Handler Edit KTP (Admin Only)
    admin_edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit_ktp", edit_ktp_admin_start)],
        states={
            ADMIN_EDIT_CHOICE: [CallbackQueryHandler(edit_ktp_admin_choice, pattern="^(admin_edit_)")],
            ADMIN_EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_ktp_admin_value)]
        },
        fallbacks=[CommandHandler("cancel", reg_cancel)]
    )
    app.add_handler(admin_edit_conv)

    # Navigation Menu Utama & Sub-Menu Callback
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^(menu_|action_|start_registration|accept_prop_|reject_prop_)"))

    # Fallback Handlers (Command Manual)
    app.add_handler(CommandHandler("ktp", cmd_ktp))
    app.add_handler(CommandHandler("networth", cmd_networth))
    app.add_handler(CommandHandler("kekayaan", cmd_networth))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("my_id", cmd_my_id))
    app.add_handler(CommandHandler("tree", cmd_tree))
    app.add_handler(CommandHandler("propose", cmd_propose))
    app.add_handler(CommandHandler("accept_proposal", cmd_accept_proposal))
    app.add_handler(CommandHandler("reject_proposal", cmd_reject_proposal))
    app.add_handler(CommandHandler("proposals_list", cmd_proposals_list))
    app.add_handler(CommandHandler("divorce", cmd_divorce))
    app.add_handler(CommandHandler("marriage_status", cmd_marriage_status))
    app.add_handler(CommandHandler("anniversary", cmd_anniversary))
    app.add_handler(CommandHandler("renew_vows", cmd_renew_vows))
    app.add_handler(CommandHandler("marriage_history", cmd_marriage_history))
    app.add_handler(CommandHandler("create_family", cmd_create_family))
    app.add_handler(CommandHandler("family", cmd_family))
    app.add_handler(CommandHandler("add_kandung", cmd_add_kandung))
    app.add_handler(CommandHandler("add_adopt", cmd_add_adopt))
    app.add_handler(CommandHandler("disown", cmd_disown))
    app.add_handler(CommandHandler("leave_family", cmd_leave_family))
    app.add_handler(CommandHandler("betray", cmd_betray))
    app.add_handler(CommandHandler("loyalty_check", cmd_loyalty_check))
    app.add_handler(CommandHandler("family_history", cmd_family_history))
    app.add_handler(CommandHandler("add_sibling", cmd_add_sibling))
    app.add_handler(CommandHandler("siblings", cmd_siblings))
    app.add_handler(CommandHandler("godparent", cmd_godparent))
    app.add_handler(CommandHandler("revoke_godparent", cmd_revoke_godparent))
    app.add_handler(CommandHandler("my_godchildren", cmd_my_godchildren))
    app.add_handler(CommandHandler("in_laws", cmd_in_laws))
    app.add_handler(CommandHandler("deposit_vault", cmd_deposit_vault))
    app.add_handler(CommandHandler("withdraw_vault", cmd_withdraw_vault))
    app.add_handler(CommandHandler("set_family_tax", cmd_set_family_tax))
    app.add_handler(CommandHandler("transfer_head", cmd_transfer_head))
    app.add_handler(CommandHandler("kick_member", cmd_kick_member))
    app.add_handler(CommandHandler("will", cmd_will))
    app.add_handler(CommandHandler("appoint_heir", cmd_appoint_heir))
    app.add_handler(CommandHandler("will_status", cmd_will_status))
    app.add_handler(CommandHandler("cancel_will", cmd_cancel_will))
    app.add_handler(CommandHandler("retire", cmd_retire))
    app.add_handler(CommandHandler("lineage_admin_panel", cmd_lineage_admin_panel))
    app.add_handler(CommandHandler("audit_kekayaan", cmd_audit_kekayaan))
    app.add_handler(CommandHandler("set_koin_admin", cmd_set_koin_admin))
    app.add_handler(CommandHandler("rename_family", cmd_rename_family))
    app.add_handler(CommandHandler("lock_family", cmd_lock_family))
    app.add_handler(CommandHandler("unlock_family", cmd_unlock_family))
    app.add_handler(CommandHandler("force_divorce", cmd_force_divorce))

    return app

def main():
    asyncio.run(init_lineage_db())
    app = build_app()
    print("🧬 Telegram Cosa Nostra Lineage Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
