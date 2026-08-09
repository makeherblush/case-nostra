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

# ==========================================
# CONVERSATION STATES FOR BUTTON INTERACTION
# ==========================================
# Register KTP States
REG_NAMA, REG_MUSE, REG_UMUR, REG_TGLLAHIR = range(4)

# Admin Edit KTP States
ADMIN_EDIT_CHOICE, ADMIN_EDIT_VALUE = range(4, 6)

# Interactive Flow States
(
    STATE_PROPOSE_TARGET, STATE_PROPOSE_TYPE,
    STATE_MANUAL_MARRIAGE_TARGET,
    STATE_CREATE_FAMILY_NAME,
    STATE_ADD_CHILD_TARGET, STATE_ADD_CHILD_TYPE,
    STATE_DISOWN_TARGET, STATE_DISOWN_REASON,
    STATE_ADD_SIBLING_TARGET, STATE_ADD_SIBLING_TYPE,
    STATE_GODPARENT_TARGET, STATE_REVOKE_GODPARENT_TARGET,
    STATE_DEPOSIT_AMOUNT, STATE_WITHDRAW_AMOUNT,
    STATE_FAMILY_TAX_RATE, STATE_TRANSFER_HEAD_TARGET,
    STATE_KICK_MEMBER_TARGET, STATE_KICK_MEMBER_REASON,
    STATE_WILL_TARGET, STATE_WILL_PCT, STATE_HEIR_TARGET,
    STATE_ADMIN_AUDIT_TARGET, STATE_ADMIN_SET_KOIN_TARGET, STATE_ADMIN_SET_KOIN_VAL,
    STATE_ADMIN_RENAME_FAM_ID, STATE_ADMIN_RENAME_FAM_NAME,
    STATE_ADMIN_LOCK_FAM_ID, STATE_ADMIN_LOCK_FAM_REASON,
    STATE_ADMIN_UNLOCK_FAM_ID, STATE_ADMIN_FORCE_DIVORCE_TARGET,
    STATE_ADMIN_EXCOMM_TARGET, STATE_ADMIN_EXCOMM_REASON,
    STATE_ADMIN_APPROVE_ACTION_ID, STATE_ADMIN_CHEAT_LOYALTY_TARGET, STATE_ADMIN_CHEAT_LOYALTY_VAL
) = range(6, 42)

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
            "Sistem arsip sedang mengalami gangguan sinyal. Coba ulangi pilihan Anda sekali lagi.",
            reply_markup=get_back_button(),
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
            "👉 Klik tombol <b>📝 Mulai Registrasi KTP</b> di bawah untuk mendaftar."
        )
        keyboard = [
            [InlineKeyboardButton("📝 Mulai Registrasi KTP", callback_data="start_register")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
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
# BUTTON NAVIGATION KEYBOARDS
# ==========================================
def get_main_menu_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📝 Utilitas & KTP", callback_data="menu_utilitas"),
            InlineKeyboardButton("💍 Pernikahan", callback_data="menu_nikah")
        ],
        [
            InlineKeyboardButton("🏛️ Struktur Keluarga", callback_data="menu_keluarga"),
            InlineKeyboardButton("⚰️ Warisan & Pensiun", callback_data="menu_warisan")
        ],
        [
            InlineKeyboardButton("🛠️ Administration Panel", callback_data="menu_admin")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_button(target_menu="menu_main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data=target_menu)]])

def get_cancel_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal & Kembali", callback_data="action_cancel_state")]])

# ==========================================
# CONVERSATION HANDLER: REGISTRATION KTP
# ==========================================
async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await user_exists(db, user_id):
            msg_text = (
                "📜 <b>IDENTITAS ANDA SUDAH TERDAFTAR!</b>\n\n"
                "KTP Citizen Anda sudah tercatat resmi di database Sindikat. Gunakan menu <b>🪪 Cek KTP Saya</b> untuk melihat profil Anda."
            )
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
            else:
                await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
            return ConversationHandler.END

    msg_text = (
        "📝 <b>PENDAFTARAN REGISTRY KTP CITIZEN COSA NOSTRA</b>\n\n"
        "Langkah 1/4: Silakan balaskan teks **NAMA LENGKAP** karakter Anda:\n"
        "<i>(Contoh: Don Vitorio Scaletta)</i>"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg_text, reply_markup=get_cancel_button(), parse_mode="HTML")
    else:
        await update.message.reply_text(msg_text, reply_markup=get_cancel_button(), parse_mode="HTML")
    return REG_NAMA

async def reg_nama(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['nama_lengkap'] = update.message.text.strip()
    await update.message.reply_text(
        "🎭 Langkah 2/4: Masukkan nama **MUSE / FACE CLAIM (FC)** yang digunakan:\n"
        "<i>(Contoh: Character Alpha / Original Concept)</i>",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return REG_MUSE

async def reg_muse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['muse'] = update.message.text.strip()
    await update.message.reply_text(
        "🎂 Langkah 3/4: Masukkan **USIA KARAKTER** Anda (Hanya Angka):\n"
        "<i>(Contoh: 28)</i>",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return REG_UMUR

async def reg_umur(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Input tidak valid! Usia harus ditulis dengan angka murni. Coba lagi:", reply_markup=get_cancel_button())
        return REG_UMUR

    context.user_data['umur'] = int(text)
    await update.message.reply_text(
        "📅 Langkah 4/4: Masukkan **TANGGAL LAHIR** karakter Anda:\n"
        "<i>(Format: DD-MM-YYYY, Contoh: 15-08-1996)</i>",
        reply_markup=get_cancel_button(),
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

    await update.message.reply_text(ktp_card, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
    return ConversationHandler.END

async def reg_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = "❌ Sesi pengisian dibatalkan."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, reply_markup=get_back_button(), parse_mode="HTML")
    else:
        await update.message.reply_text(msg, reply_markup=get_back_button(), parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# DISPLAY COMMANDS & DISPLAY FUNCTIONS
# ==========================================
async def cmd_ktp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, user_id):
            return await ensure_user_registered(update, db, user_id)

        async with db.execute(
            """SELECT nama_lengkap, muse, umur, tanggal_lahir, status_sipil, gelar_tier 
               FROM users WHERE user_id = ?""",
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()

        nama, muse, umur, tgl, status_sipil, gelar = row
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
            f"💵 <b>Koin Dompet  :</b> {koin:,} Koin\n"
            f"💎 <b>Total Net Worth:</b> <b>{net_worth:,} Koin</b>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(ktp_card, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
        else:
            await update.message.reply_text(ktp_card, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

async def cmd_networth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, user_id):
            return await ensure_user_registered(update, db, user_id)

        target_name = await get_username(db, user_id)
        net_worth, koin, bank, vault = await calculate_net_worth(db, user_id)

        status_ekonomi = "Sipil Biasa (Perlu Bekerja Keras)"
        if net_worth > 50_000_000:
            status_ekonomi = "👑 Konglomerat Elit (Miliarder)"
        elif net_worth > 10_000_000:
            status_ekonomi = "💼 Eksekutif Kartel Elit"
        elif net_worth > 1_000_000:
            status_ekonomi = "💵 Pengusaha Sukses"

        text = (
            f"💎 <b>AUDIT FINANCIAL & NET WORTH REPORT</b>\n"
            f"Subjek: <b>@{target_name}</b> (<code>{user_id}</code>)\n\n"
            f"💵 Cash Tunai   : <b>{koin:,} Koin</b>\n"
            f"🏦 Deposit Bank  : <b>{bank:,} Koin</b>\n"
            f"🏛️ Kas Keluarga : <b>{vault:,} Koin</b>\n\n"
            f"🏆 <b>TOTAL NET WORTH : {net_worth:,} Koin</b>\n"
            f"📊 Hirarki Finansial: <b>{status_ekonomi}</b>"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

async def cmd_my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (
        f"💳 <b>INFORMASI CITIZEN ID TELEGRAM</b>\n\n"
        f"ID Anda: <code>{user_id}</code>\n\n"
        f"Gunakan ID unik ini dalam transaksi diplomatik atau pendaftaran keluarga."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

async def cmd_tree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        target_name = await get_username(db, user_id)

        marriage = await get_active_marriage(db, user_id)
        spouse_str = "Belum Ada"
        if marriage:
            spouse_id = marriage[3] if marriage[2] == user_id else marriage[2]
            spouse_name = await get_username(db, spouse_id)
            spouse_str = f"@{spouse_name} (<code>{spouse_id}</code>)"

        async with db.execute(
            "SELECT child_id, relation_type FROM parent_child_relations WHERE parent_id = ? AND is_active = 1",
            (user_id,)
        ) as cursor:
            children = await cursor.fetchall()

        async with db.execute(
            """SELECT user_a_id, user_b_id, sibling_type FROM sibling_relations
               WHERE (user_a_id = ? OR user_b_id = ?) AND is_active = 1""",
            (user_id, user_id)
        ) as cursor:
            siblings = await cursor.fetchall()

        tree_lines = [
            f"🌳 <b>DIAGRAM SILSILAH KELUARGA</b>\n",
            f"👤 <b>{target_name}</b> (<code>{user_id}</code>)",
            f" ┣ 💍 Pasangan Resmi: {spouse_str}"
        ]

        if siblings:
            tree_lines.append(" ┣ 👫 <b>Garis Saudara:</b>")
            for a_id, b_id, s_type in siblings:
                s_id = b_id if a_id == user_id else a_id
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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_utilitas"), parse_mode="HTML")

# ==========================================
# INTERACTIVE PROPOSAL LIST & DIVORCE HANDLERS
# ==========================================
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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

        keyboard = []
        lines = ["💌 <b>DAFTAR PROPOSAL LAMARAN (PENDING)</b>\n"]
        for p_id, prop_id, c_at, exp_at, p_type in proposals:
            prop_name = await get_username(db, prop_id)
            rem_sec = exp_at - now_epoch
            lines.append(f"• Dari @{prop_name} (<code>{prop_id}</code>) [{p_type.capitalize()}] — Sisa Waktu: {rem_sec // 60}m {rem_sec % 60}s")
            keyboard.append([
                InlineKeyboardButton(f"✅ Terima @{prop_name}", callback_data=f"btn_accept_prop_{prop_id}"),
                InlineKeyboardButton(f"❌ Tolak @{prop_name}", callback_data=f"btn_reject_prop_{prop_id}")
            ])

        keyboard.append([InlineKeyboardButton("◀️ Kembali ke Menu Pernikahan", callback_data="menu_nikah")])
        text = "\n".join(lines)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cmd_divorce_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            msg = "🤔 Anda sedang dalam status Lajang. Tidak ada ikatan pernikahan untuk diputuskan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(msg, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

        partner_id = marriage[3] if user_id == marriage[2] else marriage[2]
        partner_name = await get_username(db, partner_id)

        text = (
            f"💔 <b>KONFIRMASI PERCERAIAN</b>\n\n"
            f"Pasangan: <b>@{partner_name}</b> (<code>{partner_id}</code>)\n"
            f"Sertifikat: <code>{marriage[1]}</code>\n\n"
            f"Pilih opsi perceraian di bawah ini:"
        )
        keyboard = [
            [InlineKeyboardButton("💔 Perceraian Biasa", callback_data="btn_do_divorce_normal")],
            [InlineKeyboardButton("⚖️ Perceraian & Bagi Harta Gono-Gini", callback_data="btn_do_divorce_split")],
            [InlineKeyboardButton("◀️ Batal & Kembali", callback_data="menu_nikah")]
        ]
        
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def process_divorce_action(update: Update, context: ContextTypes.DEFAULT_TYPE, should_split: bool):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            msg = "❌ Tidak ditemukan ikatan pernikahan aktif."
            return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

        marriage_id, cert_number, user_a, user_b, married_at, m_type = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE marriages SET status = 'divorced', divorced_at = ?, divorce_reason = 'mutual' WHERE marriage_id = ?",
            (now_epoch, marriage_id)
        )
        await db.execute("UPDATE users SET status_sipil = 'Lajang' WHERE user_id IN (?, ?)", (user_a, user_b))

        split_msg = ""
        if should_split:
            koin_a = await get_koin(db, user_a)
            koin_b = await get_koin(db, user_b)
            total_gono_gini = koin_a + koin_b
            half = total_gono_gini // 2
            
            await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (half, user_a))
            await db.execute("UPDATE users SET koin = ? WHERE user_id = ?", (half, user_b))
            split_msg = f"\n⚖️ <b>Harta Gono-Gini Dibagi Rata:</b> Total <b>{total_gono_gini:,} Koin</b> → Masing-masing dialokasikan <b>{half:,} Koin</b>."

        await db.commit()

        await update.callback_query.edit_message_text(
            f"💔 <b>PERKARA PERCERAIAN DIESAHKAN</b>\n\n"
            f"Pernikahan Anda dengan @{partner_name} (<code>{partner_id}</code>) resmi dibatalkan.\n"
            f"Sertifikat Nikah: <code>{cert_number}</code>{split_msg}\n\n"
            f"<i>Status KTP diperbarui kembali menjadi LAJANG.</i>",
            reply_markup=get_back_button("menu_nikah"),
            parse_mode="HTML"
        )

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
                "Anda tidak terikat pernikahan aktif saat ini. Pilih menu **💌 Ajukan Lamaran** untuk memulai persekutuan nikah."
            )
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

        lines = ["📜 <b>RIWAYAT MANTAN PASANGAN (10 Terakhir)</b>\n"]
        for cert, u_a, u_b, m_at, d_at, reason in rows:
            ex_id = u_b if user_id == u_a else u_a
            ex_name = await get_username(db, ex_id)
            d_date = datetime.fromtimestamp(d_at, WIB).strftime("%d %b %Y") if d_at else "-"
            lines.append(f"• Ex: @{ex_name} (<code>{ex_id}</code>)\n  📜 Cert: <code>{cert}</code>\n  🗓️ Cerai: {d_date} ({reason})\n")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

# ==========================================
# FAMILY DISPLAY COMMANDS
# ==========================================
async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = "Anda belum menjadi anggota keluarga mana pun."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        async with db.execute("SELECT family_name, head_user_id, family_vault_balance, tax_rate_percent, is_locked FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()
        
        if not fam:
            msg = "❌ Data keluarga tidak ditemukan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

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

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

async def cmd_leave_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            msg = "Anda belum tergabung dalam keluarga mana pun."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        if relation_type == "head":
            msg = "🚫 Kepala Keluarga tidak dapat mengundurkan diri secara langsung. Alihkan kepemimpinan terlebih dahulu via Otoritas Head!"
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'voluntary' WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        
        msg = "🚪 Anda resmi keluar dari struktur keluarga secara sukarela."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

async def cmd_betray(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            msg = "Tidak ada keluarga yang dapat dikhianati."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'betrayed', loyalty_score = 0 WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        
        msg = "🗡️ <b>TINDAKAN PENGKHIANATAN TERCATAT.</b>\n\nAnda membelot dari keluarga dengan status <i>betrayed</i>. Skor loyalty direset ke 0!"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(msg, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

async def cmd_loyalty_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            text = f"Anda tidak memiliki keluarga aktif."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            
        family_id, relation_type, loyalty_score = membership
        text = f"🏆 Loyalitas Citizen Anda: <b>{loyalty_score}/100</b> ({relation_type})"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        lines = ["📜 <b>RIWAYAT LOG KELUARGA (10 Terakhir)</b>\n"]
        for m_id, rel, reason, left_at in rows:
            left_date = datetime.fromtimestamp(left_at, WIB).strftime("%d %b %Y") if left_at else "-"
            m_name = await get_username(db, m_id)
            lines.append(f"• @{m_name} (<code>{m_id}</code>) ({rel}) — {reason} — {left_date}")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

async def cmd_siblings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            """SELECT user_a_id, user_b_id, sibling_type FROM sibling_relations
               WHERE (user_a_id = ? OR user_b_id = ?) AND is_active = 1""",
            (user_id, user_id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = f"👫 Citizen ID <code>{user_id}</code> belum memiliki saudara yang tercatat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        lines = [f"👫 <b>GARIS SAUDARA ANDA</b>\n"]
        for a_id, b_id, s_type in rows:
            other_id = b_id if a_id == user_id else a_id
            other_name = await get_username(db, other_id)
            label = "kandung" if s_type == "biological" else "angkat"
            lines.append(f"• @{other_name} (<code>{other_id}</code>) ({label})")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

        lines = ["🕯️ <b>DAFTAR GODCHILDREN ANDA</b>\n"]
        for r in rows:
            gc_name = await get_username(db, r[0])
            lines.append(f"• @{gc_name} (<code>{r[0]}</code>)")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

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
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")

# ==========================================
# INHERITANCE / WILL DISPLAY COMMANDS
# ==========================================
async def cmd_will_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status, updated_at, executed_at FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            text = "📜 Belum ada dokumen wasiat yang diterbitkan. Buat alokasi wasiat melalui tombol di bawah."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        will_id, status, updated_at, executed_at = will
        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            text = "📜 Dokumen wasiat Anda masih kosong."
            if update.callback_query:
                return await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        status_label = "EXECUTED" if status == "executed" else "ACTIVE"
        lines = [f"📜 <b>DOKUMEN WASIAT — Status: {status_label}</b>\n"]
        for b_id, pct in rows:
            b_name = await get_username(db, b_id)
            lines.append(f"• @{b_name} (<code>{b_id}</code>) — {pct}%")
            
        if executed_at:
            exec_date = datetime.fromtimestamp(executed_at, WIB).strftime("%d %B %Y, %H:%M WIB")
            lines.append(f"\n✅ <b>TELAH DIEKSEKUSI PADA:</b> {exec_date}")

        text = "\n".join(lines)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

async def cmd_cancel_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()

        if not will:
            msg_text = "📜 Anda tidak memiliki dokumen wasiat aktif."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        will_id, status = will
        if status == "executed":
            msg_text = "❌ Dokumen wasiat yang telah dieksekusi tidak dapat dibatalkan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        await db.execute("DELETE FROM will_beneficiaries WHERE will_id = ?", (will_id,))
        await db.execute("DELETE FROM wills WHERE will_id = ?", (will_id,))
        await db.commit()

        msg_text = "🗑️ Dokumen wasiat resmi dibatalkan dan dimusnahkan dari arsip."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
        else:
            await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

async def cmd_retire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            msg_text = "❌ Anda belum menerbitkan dokumen wasiat."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        will_id, status = will
        if status == "executed":
            msg_text = "❌ Dokumen wasiat ini telah dieksekusi sebelumnya."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            beneficiaries = await cursor.fetchall()
        if not beneficiaries:
            msg_text = "❌ Alokasi dokumen wasiat Anda masih kosong."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

        total_koin = await get_koin(db, user_id)
        if total_koin <= 0:
            msg_text = "❌ Saldo likuid Anda 0 Koin. Tidak ada aset likuid yang dapat diwariskan."
            if update.callback_query:
                return await update.callback_query.edit_message_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
            return await update.message.reply_text(msg_text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

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
            await update.callback_query.edit_message_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")
        else:
            await update.message.reply_text(text, reply_markup=get_back_button("menu_warisan"), parse_mode="HTML")

# ==========================================
# INTERACTIVE CONVERSATION HANDLERS FOR ALL FEATURES
# ==========================================

# --- 1. PROPOSE / LAMAR ---
async def interactive_propose_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💌 <b>PENGAJUAN LAMARAN NIKAH</b>\n\n"
        "Silakan balaskan **ID Telegram Target** yang ingin Anda lamar:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_PROPOSE_TARGET

async def interactive_propose_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Target harus berupa angka murni! Masukkan ulang:", reply_markup=get_cancel_button())
        return STATE_PROPOSE_TARGET

    target_id = int(text)
    user_id = update.effective_user.id

    if target_id == user_id:
        await update.message.reply_text("🤔 Tidak bisa melamar diri sendiri! Masukkan ID target lain:", reply_markup=get_cancel_button())
        return STATE_PROPOSE_TARGET

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, target_id):
            await update.message.reply_text("❌ Target belum terdaftar KTP di database! Masukkan ID lain:", reply_markup=get_cancel_button())
            return STATE_PROPOSE_TARGET

    context.user_data['propose_target_id'] = target_id

    keyboard = [
        [InlineKeyboardButton("💍 Conventional", callback_data="prop_type_conventional")],
        [InlineKeyboardButton("🥂 Modern", callback_data="prop_type_modern")],
        [InlineKeyboardButton("🗡️ Secret", callback_data="prop_type_secret")],
        [InlineKeyboardButton("❌ Batal", callback_data="action_cancel_state")]
    ]
    await update.message.reply_text(
        f"💌 Target: <code>{target_id}</code>\n\nPilih **Tipe Akad Pernikahan**:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return STATE_PROPOSE_TYPE

async def interactive_propose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    m_type = query.data.replace("prop_type_", "")
    user_id = update.effective_user.id
    target_id = context.user_data.get('propose_target_id')

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        now_epoch = int(time.time())
        expires_at = now_epoch + PROPOSAL_TTL_SECONDS
        
        await db.execute(
            "INSERT INTO marriage_proposals (proposer_id, target_id, proposal_type, status, created_at, expires_at) VALUES (?, ?, ?, 'pending', ?, ?)",
            (user_id, target_id, m_type, now_epoch, expires_at)
        )
        await db.commit()
        target_name = await get_username(db, target_id)

    await query.edit_message_text(
        f"💌 <b>PROPOSAL LAMARAN DITERBITKAN!</b>\n\n"
        f"Ditujukan ke : @{target_name} (<code>{target_id}</code>)\n"
        f"💍 Tipe Akad : <b>{m_type.capitalize()}</b>\n"
        f"⏳ Masa Berlaku: <b>10 Menit</b>\n\n"
        f"Target dapat mengecek & menerima lamaran ini via menu **💌 Proposal Pending**.",
        reply_markup=get_back_button("menu_nikah"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# --- 2. CATAT NIKAH MANUAL ---
async def interactive_manual_marriage_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💒 <b>REGISTRASI NIKAH MANUAL</b>\n\n"
        "Masukkan **ID Telegram Pasangan** Anda:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_MANUAL_MARRIAGE_TARGET

async def interactive_manual_marriage_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Pasangan harus berupa angka! Masukkan ulang:", reply_markup=get_cancel_button())
        return STATE_MANUAL_MARRIAGE_TARGET

    target_id = int(text)
    user_id = update.effective_user.id

    if target_id == user_id:
        await update.message.reply_text("🤔 Tidak bisa mendaftarkan pernikahan dengan diri sendiri!", reply_markup=get_cancel_button())
        return STATE_MANUAL_MARRIAGE_TARGET

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, target_id):
            await update.message.reply_text(f"❌ Target ID <code>{target_id}</code> belum terdaftar KTP.", parse_mode="HTML")
            return STATE_MANUAL_MARRIAGE_TARGET

        now_epoch = int(time.time())
        cert_number, sha_hash, date_formatted = generate_marriage_certificate(user_id, target_id)

        await db.execute(
            """INSERT INTO marriages (cert_number, user_a_id, user_b_id, marriage_type, status, married_at, sha256_hash)
               VALUES (?, ?, ?, 'manual_register', 'active', ?, ?)""",
            (cert_number, user_id, target_id, now_epoch, sha_hash)
        )
        await db.execute("UPDATE users SET status_sipil = 'Menikah' WHERE user_id IN (?, ?)", (user_id, target_id))
        await db.commit()

        my_name = await get_username(db, user_id)
        target_name = await get_username(db, target_id)

        msg = (
            "💒 <b>AKTA PERNIKAHAN TERCATAT RESMI!</b>\n\n"
            f"👰🤵 @{my_name} (<code>{user_id}</code>) ❤️ @{target_name} (<code>{target_id}</code>)\n"
            f"📜 No. Sertifikat : <code>{cert_number}</code>\n"
            f"🗓️ Tanggal Catat : {date_formatted}\n\n"
            "✨ <i>Status Sipil kedua belah pihak resmi diperbarui menjadi <b>MENIKAH</b>!</i>"
        )
        await update.message.reply_text(msg, reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")
    return ConversationHandler.END

# --- 3. BUAT KELUARGA ---
async def interactive_create_family_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🏛️ <b>PENDIRIAN KELUARGA BARU</b>\n\n"
        "Balaskan **NAMA KELUARGA** yang ingin Anda dirikan (3-40 karakter):",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_CREATE_FAMILY_NAME

async def interactive_create_family_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    family_name = update.message.text.strip()
    user_id = update.effective_user.id

    if not (3 <= len(family_name) <= 40):
        await update.message.reply_text("❌ Nama keluarga harus 3-40 karakter! Coba lagi:", reply_markup=get_cancel_button())
        return STATE_CREATE_FAMILY_NAME

    if family_name.upper() in BLACKLISTED_FAMILY_NAMES:
        await update.message.reply_text("🚫 Nama keluarga ini dilarang oleh sistem! Coba lagi:", reply_markup=get_cancel_button())
        return STATE_CREATE_FAMILY_NAME

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if await get_active_family_membership(db, user_id):
            await update.message.reply_text("❌ Anda sudah tergabung dalam keluarga lain!", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

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
            await update.message.reply_text("❌ Nama keluarga tersebut sudah dipatenkan oleh orang lain! Coba nama lain:", reply_markup=get_cancel_button())
            return STATE_CREATE_FAMILY_NAME

    await update.message.reply_text(
        f"🏛️ <b>KELUARGA \"{family_name}\" RESMI DIDIRIKAN!</b>\n\nSelamat! Anda kini memegang posisi Kepala Keluarga (<code>head</code>).",
        reply_markup=get_back_button("menu_keluarga"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# --- 4. TAMBAH ANAK ---
async def interactive_add_child_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👶 <b>PENDAFTARAN KETURUNAN (ANAK)</b>\n\n"
        "Balaskan **ID Telegram Anak**:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_ADD_CHILD_TARGET

async def interactive_add_child_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Anak harus angka! Masukkan ulang:", reply_markup=get_cancel_button())
        return STATE_ADD_CHILD_TARGET

    target_id = int(text)
    context.user_data['child_target_id'] = target_id

    keyboard = [
        [InlineKeyboardButton("👶 Anak Kandung", callback_data="child_type_biological")],
        [InlineKeyboardButton("🍼 Anak Angkat", callback_data="child_type_adopted")],
        [InlineKeyboardButton("❌ Batal", callback_data="action_cancel_state")]
    ]
    await update.message.reply_text(
        f"Pilih status hubungan untuk Target ID <code>{target_id}</code>:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return STATE_ADD_CHILD_TYPE

async def interactive_add_child_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rel_type = query.data.replace("child_type_", "")
    user_id = update.effective_user.id
    child_id = context.user_data.get('child_target_id')

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO parent_child_relations (parent_id, child_id, relation_type, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (user_id, child_id, rel_type, now_epoch, user_id)
        )
        await db.commit()

        child_name = await get_username(db, child_id)
        label = "anak kandung" if rel_type == "biological" else "anak angkat"

    await query.edit_message_text(
        f"👶 @{child_name} (<code>{child_id}</code>) resmi tercatat sebagai <b>{label}</b> Anda!",
        reply_markup=get_back_button("menu_keluarga"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# --- 5. DISOWN (HAPUS ANAK) ---
async def interactive_disown_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "⚔️ <b>DISOWN (HAPUS KETURUNAN)</b>\n\n"
        "Balaskan **ID Telegram Anak** yang ingin didisown:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_DISOWN_TARGET

async def interactive_disown_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Target harus berupa angka!", reply_markup=get_cancel_button())
        return STATE_DISOWN_TARGET

    context.user_data['disown_target_id'] = int(text)
    await update.message.reply_text("Balaskan **ALASAN DISOWN**:", reply_markup=get_cancel_button(), parse_mode="HTML")
    return STATE_DISOWN_REASON

async def interactive_disown_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = update.message.text.strip()
    user_id = update.effective_user.id
    child_id = context.user_data.get('disown_target_id')

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        now_epoch = int(time.time())
        cursor = await db.execute(
            """UPDATE parent_child_relations SET is_active = 0, disowned_at = ?, disowned_reason = ?
               WHERE parent_id = ? AND child_id = ? AND is_active = 1""",
            (now_epoch, reason, user_id, child_id)
        )
        await db.commit()

        if cursor.rowcount == 0:
            await update.message.reply_text("❌ Tidak ditemukan relasi keturunan aktif dengan target ID tersebut.", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

    await update.message.reply_text(
        f"⚔️ Citizen ID <code>{child_id}</code> resmi <b>didisown (dihapus dari silsilah)</b>.\nAlasan: {reason}",
        reply_markup=get_back_button("menu_keluarga"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# --- 6. VAULT DEPOSIT & WITHDRAW ---
async def interactive_deposit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💰 <b>DEPOSIT KOIN KE VAULT KELUARGA</b>\n\n"
        "Balaskan **JUMLAH KOIN** yang ingin didepositkan:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_DEPOSIT_AMOUNT

async def interactive_deposit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Jumlah deposit harus berupa angka positif!", reply_markup=get_cancel_button())
        return STATE_DEPOSIT_AMOUNT

    amount = int(text)
    user_id = update.effective_user.id

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        membership = await get_active_family_membership(db, user_id)
        if not membership:
            await update.message.reply_text("❌ Anda belum bergabung dalam keluarga!", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

        family_id = membership[0]
        user_koin = await get_koin(db, user_id)
        if user_koin < amount:
            await update.message.reply_text(f"❌ Saldo dompet Anda tidak cukup! (Saldo: {user_koin:,} Koin)", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

        await add_koin(db, user_id, -amount)
        await db.execute("UPDATE families SET family_vault_balance = family_vault_balance + ? WHERE family_id = ?", (amount, family_id))
        await db.commit()

    await update.message.reply_text(f"💰 Berhasil mendepositkan <b>{amount:,} Koin</b> ke Vault Keluarga!", reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
    return ConversationHandler.END

async def interactive_withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "💸 <b>PENARIKAN KAS VAULT KELUARGA (KHUSUS HEAD)</b>\n\n"
        "Balaskan **JUMLAH KOIN** yang ingin ditarik:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_WITHDRAW_AMOUNT

async def interactive_withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Jumlah penarikan harus angka positif!", reply_markup=get_cancel_button())
        return STATE_WITHDRAW_AMOUNT

    amount = int(text)
    user_id = update.effective_user.id

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        membership = await get_active_family_membership(db, user_id)
        if not membership or membership[1] != "head":
            await update.message.reply_text("🚫 Penarikan Vault hanya dapat dilakukan oleh Kepala Keluarga (Head).", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

        family_id = membership[0]
        async with db.execute("SELECT family_vault_balance FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()
            vault_balance = fam[0] if fam else 0

        if vault_balance < amount:
            await update.message.reply_text(f"❌ Kas Vault Keluarga tidak mencukupi! (Kas Vault: {vault_balance:,} Koin)", reply_markup=get_back_button("menu_keluarga"))
            return ConversationHandler.END

        await db.execute("UPDATE families SET family_vault_balance = family_vault_balance - ? WHERE family_id = ?", (amount, family_id))
        await add_koin(db, user_id, amount)
        await db.commit()

    await update.message.reply_text(f"💸 Berhasil menarik <b>{amount:,} Koin</b> dari Vault Keluarga!", reply_markup=get_back_button("menu_keluarga"), parse_mode="HTML")
    return ConversationHandler.END

# --- 7. ATUR SURAT WASIAT INTERAKTIF ---
async def interactive_will_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📝 <b>PENGATURAN SURAT WASIAT</b>\n\n"
        "Balaskan **ID Telegram Penerima Waris**:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_WILL_TARGET

async def interactive_will_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Target harus berupa angka!", reply_markup=get_cancel_button())
        return STATE_WILL_TARGET

    target_id = int(text)
    user_id = update.effective_user.id

    if target_id == user_id:
        await update.message.reply_text("🤔 Tidak bisa mewariskan kepada diri sendiri! Masukkan ID lain:", reply_markup=get_cancel_button())
        return STATE_WILL_TARGET

    context.user_data['will_beneficiary_id'] = target_id
    await update.message.reply_text(f"Balaskan **PERSENTASE WARISAN** untuk ID <code>{target_id}</code> (Contoh: 50):", reply_markup=get_cancel_button(), parse_mode="HTML")
    return STATE_WILL_PCT

async def interactive_will_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        pct = float(text)
        if not (0 < pct <= 100):
            raise ValueError()
    except ValueError:
        await update.message.reply_text("❌ Persentase harus angka antara 1 - 100!", reply_markup=get_cancel_button())
        return STATE_WILL_PCT

    user_id = update.effective_user.id
    b_id = context.user_data.get('will_beneficiary_id')

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
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

        await db.execute(
            "INSERT INTO will_beneficiaries (will_id, beneficiary_id, percent) VALUES (?, ?, ?)",
            (will_id, b_id, pct)
        )
        await db.commit()
        b_name = await get_username(db, b_id)

    await update.message.reply_text(
        f"📜 <b>SURAT WASIAT DIPERBARUI!</b>\n\nPenerima: @{b_name} (<code>{b_id}</code>)\nAlokasi: <b>{pct}%</b>",
        reply_markup=get_back_button("menu_warisan"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# --- 8. AHLI WARIS TUNGGAL ---
async def interactive_heir_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👑 <b>PENUNJUKAN AHLI WARIS TUNGGAL (100%)</b>\n\n"
        "Balaskan **ID Telegram Ahli Waris**:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return STATE_HEIR_TARGET

async def interactive_heir_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Target harus berupa angka!", reply_markup=get_cancel_button())
        return STATE_HEIR_TARGET

    heir_id = int(text)
    user_id = update.effective_user.id

    if heir_id == user_id:
        await update.message.reply_text("❌ Tidak bisa menunjuk diri sendiri!", reply_markup=get_cancel_button())
        return STATE_HEIR_TARGET

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
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

        await db.execute(
            "INSERT INTO will_beneficiaries (will_id, beneficiary_id, percent) VALUES (?, ?, 100.0)",
            (will_id, heir_id)
        )
        await db.commit()
        heir_name = await get_username(db, heir_id)

    await update.message.reply_text(
        f"👑 @{heir_name} (<code>{heir_id}</code>) resmi ditunjuk sebagai **Ahli Waris Tunggal (100%)**!",
        reply_markup=get_back_button("menu_warisan"),
        parse_mode="HTML"
    )
    return ConversationHandler.END

# ==========================================
# SYSTEM MAIN MENU CALLBACK HANDLER
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚔️ <b>SELAMAT DATANG DI PORTAL SISTEM SILSILAH LINEAGE COSA NOSTRA</b> 🏛️\n\n"
        "<i>\"Keluarga bukan hanya sekadar hubungan darah, melainkan tentang kehormatan, aliansi kekayaan, dan loyalitas tanpa batas.\"</i>\n\n"
        "Selamat datang, Citizen! Bot ini merupakan pusat administrasi digital untuk seluruh jajaran dinasti Cosa Nostra Network.\n\n"
        "Silakan pilih tombol navigasi di bawah ini untuk memulai interaksi:"
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

    # ------------------------------------------
    # SUB-MENU 1: UTILITAS & KTP
    # ------------------------------------------
    elif data == "menu_utilitas":
        text = (
            "📝 <b>SUB-MENU PENDAFTARAN & REGISTRY CITIZEN</b>\n\n"
            "Pilih tombol di bawah untuk mengisi KTP atau mengakses informasi finansial:"
        )
        keyboard = [
            [InlineKeyboardButton("📝 Mulai Registrasi KTP", callback_data="start_register")],
            [InlineKeyboardButton("🪪 Cek KTP Saya", callback_data="action_ktp"), InlineKeyboardButton("💎 Net Worth Report", callback_data="action_networth")],
            [InlineKeyboardButton("🎁 Klaim Tunjangan Daily", callback_data="action_daily"), InlineKeyboardButton("💳 Cek ID Telegram", callback_data="action_my_id")],
            [InlineKeyboardButton("🌳 Diagram Silsilah", callback_data="action_tree")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ------------------------------------------
    # SUB-MENU 2: PERNIKAHAN
    # ------------------------------------------
    elif data == "menu_nikah":
        text = (
            "💍 <b>SUB-MENU PERSEKUTUAN NIKAH</b>\n\n"
            "Pilih fitur pernikahan yang ingin diakses secara langsung:"
        )
        keyboard = [
            [InlineKeyboardButton("💌 Ajukan Lamaran (Propose)", callback_data="start_propose"), InlineKeyboardButton("💒 Catat Nikah Manual", callback_data="start_manual_marriage")],
            [InlineKeyboardButton("💍 Status Nikah", callback_data="action_marriage_status"), InlineKeyboardButton("💌 Proposal Pending", callback_data="action_proposals_list")],
            [InlineKeyboardButton("💖 Anniversary", callback_data="action_anniversary"), InlineKeyboardButton("🕊️ Perbarui Janji", callback_data="action_renew_vows")],
            [InlineKeyboardButton("📜 Mantan Pasangan", callback_data="action_marriage_history"), InlineKeyboardButton("💔 Perceraian (Divorce)", callback_data="action_divorce_menu")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ------------------------------------------
    # SUB-MENU 3: STRUKTUR KELUARGA
    # ------------------------------------------
    elif data == "menu_keluarga":
        text = (
            "🏛️ <b>SUB-MENU STRUKTUR KELUARGA & DINASTI</b>\n\n"
            "Pilih fitur pengelolaan keluarga di bawah ini:"
        )
        keyboard = [
            [InlineKeyboardButton("➕ Mendirikan Keluarga", callback_data="start_create_family"), InlineKeyboardButton("👶 Tambah Anak", callback_data="start_add_child")],
            [InlineKeyboardButton("🏛️ Cek Info Keluarga", callback_data="action_family"), InlineKeyboardButton("🏆 Cek Loyalitas Saya", callback_data="action_loyalty_check")],
            [InlineKeyboardButton("💰 Setor Kas Vault", callback_data="start_deposit"), InlineKeyboardButton("💸 Tarik Kas Vault", callback_data="start_withdraw")],
            [InlineKeyboardButton("⚔️ Hapus Anak (Disown)", callback_data="start_disown")],
            [InlineKeyboardButton("📜 Log Riwayat Anggota", callback_data="action_family_history"), InlineKeyboardButton("👫 Garis Saudara", callback_data="action_siblings")],
            [InlineKeyboardButton("🕯️ Godchildren Saya", callback_data="action_my_godchildren"), InlineKeyboardButton("👪 Struktur In-Laws", callback_data="action_in_laws")],
            [InlineKeyboardButton("🚪 Keluar Keluarga", callback_data="action_leave_family"), InlineKeyboardButton("🗡️ Membelot (Betray)", callback_data="action_betray")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ------------------------------------------
    # SUB-MENU 4: WARISAN & PENSIUN
    # ------------------------------------------
    elif data == "menu_warisan":
        text = (
            "⚰️ <b>SUB-MENU WARISAN & PENSIUN</b>\n\n"
            "Pilih fitur pengelolaan aset dan dokumen warisan:"
        )
        keyboard = [
            [InlineKeyboardButton("📝 Atur Surat Wasiat", callback_data="start_will"), InlineKeyboardButton("👑 Tunjuk Ahli Waris Tunggal", callback_data="start_heir")],
            [InlineKeyboardButton("📜 Cek Status Wasiat", callback_data="action_will_status"), InlineKeyboardButton("🗑️ Batalkan Wasiat", callback_data="action_cancel_will")],
            [InlineKeyboardButton("⚰️ Eksekusi Pensiun", callback_data="action_retire")],
            [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ------------------------------------------
    # SUB-MENU 5: ADMINISTRATION PANEL
    # ------------------------------------------
    elif data == "menu_admin":
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            await ensure_all_tables_exist(db)
            tier = await check_admin_tier(db, user_id)
            if tier == 0:
                return await query.edit_message_text(
                    "🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.",
                    reply_markup=get_back_button("menu_main"),
                    parse_mode="HTML"
                )

            text = f"🛠️ <b>LINEAGE ADMIN PANEL (Tier {tier})</b>\n\nPilih fitur kontrol admin:"
            keyboard = [
                [InlineKeyboardButton("✏️ Edit KTP Warga", callback_data="start_admin_edit_ktp")],
                [InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")]
            ]
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ------------------------------------------
    # BUTTON ACCEPT / REJECT PROPOSAL CALLBACKS
    # ------------------------------------------
    elif data.startswith("btn_accept_prop_"):
        proposer_id = int(data.replace("btn_accept_prop_", ""))
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            await ensure_all_tables_exist(db)
            now_epoch = int(time.time())
            cert_number, sha_hash, date_formatted = generate_marriage_certificate(proposer_id, user_id)
            
            await db.execute(
                """INSERT INTO marriages (cert_number, user_a_id, user_b_id, marriage_type, status, married_at, sha256_hash)
                   VALUES (?, ?, ?, 'conventional', 'active', ?, ?)""",
                (cert_number, proposer_id, user_id, now_epoch, sha_hash)
            )
            await db.execute("UPDATE marriage_proposals SET status = 'accepted', responded_at = ? WHERE proposer_id = ? AND target_id = ? AND status = 'pending'", (now_epoch, proposer_id, user_id))
            await db.execute("UPDATE users SET status_sipil = 'Menikah' WHERE user_id IN (?, ?)", (user_id, proposer_id))
            await db.commit()

            proposer_name = await get_username(db, proposer_id)
            my_name = await get_username(db, user_id)

        await query.edit_message_text(
            f"💒 <b>PERSEKUTUAN NIKAH RESMI DIESAHKAN!</b>\n\n"
            f"👰🤵 @{proposer_name} (<code>{proposer_id}</code>) ❤️ @{my_name} (<code>{user_id}</code>)\n"
            f"📜 No. Sertifikat: <code>{cert_number}</code>\n"
            f"🗓️ Tanggal    : {date_formatted}",
            reply_markup=get_back_button("menu_nikah"),
            parse_mode="HTML"
        )

    elif data.startswith("btn_reject_prop_"):
        proposer_id = int(data.replace("btn_reject_prop_", ""))
        user_id = update.effective_user.id
        async with get_db_connection() as db:
            await ensure_all_tables_exist(db)
            now_epoch = int(time.time())
            await db.execute(
                "UPDATE marriage_proposals SET status = 'rejected', responded_at = ? WHERE proposer_id = ? AND target_id = ? AND status = 'pending'",
                (now_epoch, proposer_id, user_id)
            )
            await db.commit()

        await query.edit_message_text(f"💔 Proposal lamaran dari ID <code>{proposer_id}</code> resmi ditolak.", reply_markup=get_back_button("menu_nikah"), parse_mode="HTML")

    # ------------------------------------------
    # BUTTON DIVORCE EXECUTION CALLBACKS
    # ------------------------------------------
    elif data == "btn_do_divorce_normal":
        await process_divorce_action(update, context, should_split=False)
    elif data == "btn_do_divorce_split":
        await process_divorce_action(update, context, should_split=True)

    # ------------------------------------------
    # DIRECT ACTION BUTTON DISPATCHER
    # ------------------------------------------
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
        elif action == "marriage_history":
            await cmd_marriage_history(fake_update, context)
        elif action == "divorce_menu":
            await cmd_divorce_menu(fake_update, context)
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
# CONVERSATION HANDLER: ADMIN EDIT KTP
# ==========================================
async def edit_ktp_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🛠️ <b>ADMINISTRATION CONTROL — EDIT KTP WARGA</b>\n\n"
        "Balaskan **ID Telegram Target** yang ingin diubah datanya:",
        reply_markup=get_cancel_button(),
        parse_mode="HTML"
    )
    return ADMIN_EDIT_CHOICE

async def edit_ktp_admin_choice_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ ID Target harus berupa angka!", reply_markup=get_cancel_button())
        return ADMIN_EDIT_CHOICE

    target_id = int(text)
    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        if not await user_exists(db, target_id):
            await update.message.reply_text("❌ Target ID tidak ditemukan!", reply_markup=get_cancel_button())
            return ADMIN_EDIT_CHOICE

    context.user_data['admin_edit_target_id'] = target_id

    keyboard = [
        [InlineKeyboardButton("👤 Nama Lengkap", callback_data="admin_edit_nama_lengkap")],
        [InlineKeyboardButton("🎭 Muse / Avatar", callback_data="admin_edit_muse")],
        [InlineKeyboardButton("🎂 Umur Karakter", callback_data="admin_edit_umur")],
        [InlineKeyboardButton("📅 Tanggal Lahir", callback_data="admin_edit_tanggal_lahir")],
        [InlineKeyboardButton("❌ Batal", callback_data="action_cancel_state")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"🛠️ Target ID: <code>{target_id}</code>\nPilih bidang variabel yang ingin diperbarui:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    return ADMIN_EDIT_VALUE

async def edit_ktp_admin_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    field_map = {
        "admin_edit_nama_lengkap": ("nama_lengkap", "Masukkan **NAMA LENGKAP** baru:"),
        "admin_edit_muse": ("muse", "Masukkan **MUSE / AVATAR** baru:"),
        "admin_edit_umur": ("umur", "Masukkan **USIA** baru (Format Angka):"),
        "admin_edit_tanggal_lahir": ("tanggal_lahir", "Masukkan **TANGGAL LAHIR** baru (DD-MM-YYYY):")
    }

    if data in field_map:
        db_col, prompt = field_map[data]
        context.user_data['admin_edit_col'] = db_col
        await query.edit_message_text(f"✏️ {prompt}", reply_markup=get_cancel_button(), parse_mode="HTML")
        return ADMIN_EDIT_VALUE

    return ADMIN_EDIT_CHOICE

async def edit_ktp_admin_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target_id = context.user_data.get('admin_edit_target_id')
    target_col = context.user_data.get('admin_edit_col')
    new_value = update.message.text.strip()

    if target_col == "umur":
        if not new_value.isdigit():
            await update.message.reply_text("❌ Input harus berupa angka! Masukkan ulang:", reply_markup=get_cancel_button())
            return ADMIN_EDIT_VALUE
        new_value = int(new_value)

    async with get_db_connection() as db:
        await ensure_all_tables_exist(db)
        await db.execute(f"UPDATE users SET {target_col} = ? WHERE user_id = ?", (new_value, target_id))
        await db.commit()

    await update.message.reply_text("✅ Data KTP Warga Berhasil Diperbarui!", reply_markup=get_back_button("menu_admin"), parse_mode="HTML")
    return ConversationHandler.END

# ==========================================
# MAIN FUNCTION BUILDER
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    # 1. Registration Handler
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", reg_start),
            CallbackQueryHandler(reg_start, pattern="^start_register$")
        ],
        states={
            REG_NAMA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_nama)],
            REG_MUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_muse)],
            REG_UMUR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_umur)],
            REG_TGLLAHIR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_tgl_lahir)]
        },
        fallbacks=[
            CommandHandler("cancel", reg_cancel),
            CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")
        ]
    )
    app.add_handler(reg_conv)

    # 2. Interactive Propose Handler
    propose_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_propose_start, pattern="^start_propose$")],
        states={
            STATE_PROPOSE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_propose_target)],
            STATE_PROPOSE_TYPE: [CallbackQueryHandler(interactive_propose_type, pattern="^prop_type_")]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(propose_conv)

    # 3. Interactive Manual Marriage
    manual_mrg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_manual_marriage_start, pattern="^start_manual_marriage$")],
        states={
            STATE_MANUAL_MARRIAGE_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_manual_marriage_target)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(manual_mrg_conv)

    # 4. Interactive Create Family
    create_fam_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_create_family_start, pattern="^start_create_family$")],
        states={
            STATE_CREATE_FAMILY_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_create_family_name)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(create_fam_conv)

    # 5. Interactive Add Child
    add_child_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_add_child_start, pattern="^start_add_child$")],
        states={
            STATE_ADD_CHILD_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_add_child_target)],
            STATE_ADD_CHILD_TYPE: [CallbackQueryHandler(interactive_add_child_type, pattern="^child_type_")]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(add_child_conv)

    # 6. Interactive Disown
    disown_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_disown_start, pattern="^start_disown$")],
        states={
            STATE_DISOWN_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_disown_target)],
            STATE_DISOWN_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_disown_reason)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(disown_conv)

    # 7. Interactive Vault Deposit & Withdraw
    deposit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_deposit_start, pattern="^start_deposit$")],
        states={
            STATE_DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_deposit_amount)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(deposit_conv)

    withdraw_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_withdraw_start, pattern="^start_withdraw$")],
        states={
            STATE_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_withdraw_amount)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(withdraw_conv)

    # 8. Interactive Will Setup & Heir
    will_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_will_start, pattern="^start_will$")],
        states={
            STATE_WILL_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_will_target)],
            STATE_WILL_PCT: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_will_pct)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(will_conv)

    heir_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(interactive_heir_start, pattern="^start_heir$")],
        states={
            STATE_HEIR_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, interactive_heir_target)]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(heir_conv)

    # 9. Admin Edit KTP Conversation
    admin_edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_ktp_admin_start, pattern="^start_admin_edit_ktp$")],
        states={
            ADMIN_EDIT_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_ktp_admin_choice_target)],
            ADMIN_EDIT_VALUE: [
                CallbackQueryHandler(edit_ktp_admin_choice, pattern="^admin_edit_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_ktp_admin_value)
            ]
        },
        fallbacks=[CallbackQueryHandler(reg_cancel, pattern="^action_cancel_state$")]
    )
    app.add_handler(admin_edit_conv)

    # Navigation Menu Utama & Sub-Menu Callbacks
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_callback_handler, pattern="^(menu_|action_|btn_)"))

    # Direct Commands fallback
    app.add_handler(CommandHandler("ktp", cmd_ktp))
    app.add_handler(CommandHandler("networth", cmd_networth))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("my_id", cmd_my_id))
    app.add_handler(CommandHandler("tree", cmd_tree))

    return app

def main():
    asyncio.run(init_lineage_db())
    app = build_app()
    print("🧬 Telegram Cosa Nostra Lineage Bot Running (Full Interactive Buttons)...")
    app.run_polling()

if __name__ == "__main__":
    main()
