import os
import time
import random
import hashlib
import asyncio
import logging
import aiosqlite
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes
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

BLACKLISTED_FAMILY_NAMES = {
    "ADMIN", "ADMINISTRATOR", "OFFICIAL", "SYSTEM", "MOD", "MODERATOR",
    "COSA NOSTRA", "COSA_NOSTRA", "COSA NOSTRA OFFICIAL", "OWNER", "STAFF",
}

MY_PERMANENT_OWNER_ID = 8396793986  # sinkron dengan operation_bot.py

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
# DATABASE INITIALIZATION
# ==========================================
async def init_lineage_db():
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_family_member
            ON family_members(user_id) WHERE is_active = 1
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_fm_family ON family_members(family_id)")

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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_marriage_a
            ON marriages(user_a_id) WHERE status = 'active'
        """)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_active_marriage_b
            ON marriages(user_b_id) WHERE status = 'active'
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS marriage_proposals (
                proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposer_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                responded_at INTEGER
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_proposal_target ON marriage_proposals(target_id, status)")

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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pcr_parent ON parent_child_relations(parent_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_pcr_child ON parent_child_relations(child_id)")

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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sibling_a ON sibling_relations(user_a_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_sibling_b ON sibling_relations(user_b_id)")

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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_godparent ON godparent_relations(godparent_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_godchild ON godparent_relations(godchild_id)")

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

async def post_init(application):
    await init_lineage_db()
    application.create_task(expire_proposals_loop())

async def expire_proposals_loop():
    while True:
        try:
            now_epoch = int(time.time())
            async with get_db_connection() as db:
                await db.execute(
                    "UPDATE marriage_proposals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                    (now_epoch,)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"[lineage_bot] expire_proposals_loop error: {e}")
        await asyncio.sleep(PROPOSAL_EXPIRY_CHECK_INTERVAL)

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>Sistem Lineage sedang mengalami gangguan</b>\n\n"
            "Coba lagi dalam beberapa detik, atau hubungi Admin jika masalah berlanjut.\n\n"
            "<i>Tip: Pastikan format command Anda benar (lihat /start)</i>",
            parse_mode="HTML"
        )

# ==========================================
# SHARED HELPERS & REGISTRATION CHECK
# ==========================================
async def check_admin_tier(db, user_id: int) -> int:
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4
    async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

async def user_exists(db, user_id: int) -> bool:
    async with db.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)) as cursor:
        return (await cursor.fetchone()) is not None

async def ensure_user_registered(update: Update, db, user_id: int) -> bool:
    """Mengecek apakah user terdaftar. Jika belum, kirim instruksi pendaftaran."""
    if not await user_exists(db, user_id):
        await update.message.reply_text(
            "❌ <b>Anda belum terdaftar di database Cosa Nostra!</b>\n\n"
            "Untuk dapat menggunakan fitur keluarga, pernikahan, dan warisan, Anda harus mendaftar terlebih dahulu.\n\n"
            "👉 <b>Cara Mendaftar:</b>\n"
            "Ketik perintah <code>/register</code> di bot lain (atau minta Admin untuk mendaftarkan Anda).",
            parse_mode="HTML"
        )
        return False
    return True

async def get_username(db, user_id: int) -> str:
    async with db.execute("SELECT username FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] else str(user_id)

async def get_koin(db, user_id: int) -> int:
    async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row else 0

async def add_koin(db, user_id: int, amount: int):
    await db.execute("UPDATE users SET koin = koin + ? WHERE user_id = ?", (amount, user_id))

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
    async with db.execute(
        """SELECT marriage_id, cert_number, user_a_id, user_b_id, married_at
           FROM marriages
           WHERE status = 'active' AND (user_a_id = ? OR user_b_id = ?)""",
        (user_id, user_id)
    ) as cursor:
        return await cursor.fetchone()

async def get_active_family_membership(db, user_id: int):
    async with db.execute(
        """SELECT family_id, relation_type, loyalty_score FROM family_members
           WHERE user_id = ? AND is_active = 1""",
        (user_id,)
    ) as cursor:
        return await cursor.fetchone()

async def is_ancestor(db, potential_ancestor_id: int, of_user_id: int, max_depth: int = 20) -> bool:
    visited = set()
    frontier = [of_user_id]
    depth = 0
    while frontier and depth < max_depth:
        placeholders = ",".join("?" for _ in frontier)
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
    return False

# ==========================================
# MARRIAGE COMMANDS
# ==========================================
async def cmd_propose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text(
            "💍 Format: <code>/propose [user_id]</code>\n\nContoh: <code>/propose 123456789</code>",
            parse_mode="HTML"
        )
    if target_id == user_id:
        return await update.message.reply_text("❌ Tidak bisa melamar diri sendiri.")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, target_id):
            return await update.message.reply_text(
                f"❌ <b>User {target_id} belum terdaftar</b>\n\n"
                f"Minta mereka untuk mendaftar terlebih dahulu dengan <code>/register</code> di bot lain.",
                parse_mode="HTML"
            )

        if await get_active_marriage(db, user_id):
            return await update.message.reply_text(
                "💍 <b>Anda sudah menikah, tidak bisa melamar lagi!</b>\n\n"
                "Gunakan <code>/divorce</code> jika ingin keluar dari pernikahan sebelumnya.",
                parse_mode="HTML"
            )
        if await get_active_marriage(db, target_id):
            return await update.message.reply_text(
                f"💍 <b>User {target_id} sudah memiliki pasangan.</b>\n\n"
                f"Tunggu sampai mereka cerai, atau pilih target lain. 💕",
                parse_mode="HTML"
            )

        if await is_ancestor(db, target_id, user_id) or await is_ancestor(db, user_id, target_id):
            return await update.message.reply_text("🚫 Tidak bisa melamar anggota keluarga kandung/leluhur sendiri.")

        since_epoch = int(time.time()) - 86400
        async with db.execute(
            "SELECT COUNT(*) FROM marriage_proposals WHERE proposer_id = ? AND created_at > ?",
            (user_id, since_epoch)
        ) as cursor:
            count_today = (await cursor.fetchone())[0]
        if count_today >= MAX_PROPOSALS_PER_DAY:
            return await update.message.reply_text(f"🚫 Anda sudah mengirim {MAX_PROPOSALS_PER_DAY} lamaran hari ini. Coba lagi besok.")

        async with db.execute(
            "SELECT 1 FROM marriage_proposals WHERE proposer_id = ? AND target_id = ? AND status = 'pending'",
            (user_id, target_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("⏳ Anda masih punya lamaran pending ke target ini.")

        now_epoch = int(time.time())
        expires_at = now_epoch + PROPOSAL_TTL_SECONDS
        await db.execute(
            "INSERT INTO marriage_proposals (proposer_id, target_id, status, created_at, expires_at) VALUES (?, ?, 'pending', ?, ?)",
            (user_id, target_id, now_epoch, expires_at)
        )
        await db.commit()

        target_name = await get_username(db, target_id)
        await update.message.reply_text(
            f"💌 <b>LAMARAN TERKIRIM!</b>\n\n"
            f"Ditujukan ke: @{target_name} (<code>{target_id}</code>)\n"
            f"⏳ <b>Berlaku selama 10 menit</b>\n\n"
            f"Mereka bisa terima dengan <code>/accept_proposal {user_id}</code>\n"
            f"atau tolak dengan <code>/reject_proposal {user_id}</code>",
            parse_mode="HTML"
        )

async def cmd_accept_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    proposer_id = parse_target_id(context)
    if proposer_id is None:
        return await update.message.reply_text("❌ Format: <code>/accept_proposal [proposer_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        async with db.execute(
            """SELECT proposal_id, expires_at FROM marriage_proposals
               WHERE proposer_id = ? AND target_id = ? AND status = 'pending'
               ORDER BY proposal_id DESC LIMIT 1""",
            (proposer_id, user_id)
        ) as cursor:
            proposal = await cursor.fetchone()

        if not proposal:
            return await update.message.reply_text(
                f"💔 <b>Tidak ada lamaran dari {proposer_id}</b>\n\n"
                f"Cek dengan <code>/marriage_status</code> untuk detail pernikahan Anda.",
                parse_mode="HTML"
            )

        proposal_id, expires_at = proposal
        if expires_at < now_epoch:
            await db.execute("UPDATE marriage_proposals SET status = 'expired' WHERE proposal_id = ?", (proposal_id,))
            await db.commit()
            return await update.message.reply_text("⏳ Lamaran sudah kadaluarsa (timeout 10 menit). Minta mereka kirim ulang lamaran.")

        if await get_active_marriage(db, user_id) or await get_active_marriage(db, proposer_id):
            await db.execute("UPDATE marriage_proposals SET status = 'rejected', responded_at = ? WHERE proposal_id = ?", (now_epoch, proposal_id))
            await db.commit()
            return await update.message.reply_text("❌ Salah satu pihak sudah menikah duluan. Lamaran dibatalkan.")

        cert_number, sha_hash, date_formatted = generate_marriage_certificate(proposer_id, user_id)
        await db.execute(
            """INSERT INTO marriages (cert_number, user_a_id, user_b_id, marriage_type, status, married_at, sha256_hash)
               VALUES (?, ?, ?, 'conventional', 'active', ?, ?)""",
            (cert_number, proposer_id, user_id, now_epoch, sha_hash)
        )
        await db.execute("UPDATE marriage_proposals SET status = 'accepted', responded_at = ? WHERE proposal_id = ?", (now_epoch, proposal_id))
        await db.commit()

        proposer_name = await get_username(db, proposer_id)
        my_name = await get_username(db, user_id)
        await update.message.reply_text(
            f"💒 <b>PERNIKAHAN RESMI TERCATAT!</b>\n\n"
            f"👰🤵 @{proposer_name} (<code>{proposer_id}</code>) ❤️ @{my_name} (<code>{user_id}</code>)\n"
            f"📜 Sertifikat: <code>{cert_number}</code>\n"
            f"🗓️ {date_formatted}\n\n"
            f"Gunakan <code>/marriage_status</code> untuk mengecek detail status pernikahan Anda.",
            parse_mode="HTML"
        )

async def cmd_reject_proposal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    proposer_id = parse_target_id(context)
    if proposer_id is None:
        return await update.message.reply_text("❌ Format: <code>/reject_proposal [proposer_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        now_epoch = int(time.time())
        cursor = await db.execute(
            """UPDATE marriage_proposals SET status = 'rejected', responded_at = ?
               WHERE proposer_id = ? AND target_id = ? AND status = 'pending'""",
            (now_epoch, proposer_id, user_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text(
                f"💔 <b>Tidak ada lamaran dari {proposer_id}</b>\n\n"
                f"Cek dengan <code>/marriage_status</code> untuk detail status Anda.",
                parse_mode="HTML"
            )
        await update.message.reply_text(f"💔 Lamaran dari <code>{proposer_id}</code> resmi ditolak.", parse_mode="HTML")

async def cmd_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            return await update.message.reply_text("❌ Anda belum menikah.")

        marriage_id, cert_number, user_a, user_b, married_at = marriage
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE marriages SET status = 'divorced', divorced_at = ?, divorce_reason = 'mutual' WHERE marriage_id = ?",
            (now_epoch, marriage_id)
        )
        await db.commit()

        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)
        await update.message.reply_text(
            f"💔 <b>PERCERAIAN TERCATAT</b>\n\n"
            f"Pernikahan dengan @{partner_name} (<code>{partner_id}</code>) telah resmi berakhir.\n"
            f"Sertifikat: <code>{cert_number}</code>\n\n"
            f"<i>Semoga sukses di masa depan...</i> 💔",
            parse_mode="HTML"
        )

async def cmd_marriage_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            return await update.message.reply_text(
                "💍 <b>Status: LAJANG</b>\n\n"
                "Gunakan <code>/propose [user_id]</code> untuk melamar seseorang! 💕",
                parse_mode="HTML"
            )

        marriage_id, cert_number, user_a, user_b, married_at = marriage
        partner_id = user_b if user_id == user_a else user_a
        partner_name = await get_username(db, partner_id)
        married_date = datetime.fromtimestamp(married_at, WIB).strftime("%d %B %Y, %H:%M WIB")

        await update.message.reply_text(
            f"💍 <b>STATUS PERNIKAHAN</b>\n\n"
            f"Pasangan: <b>@{partner_name}</b> (<code>{partner_id}</code>)\n"
            f"Sertifikat: <code>{cert_number}</code>\n"
            f"Menikah sejak: {married_date}\n\n"
            f"Gunakan <code>/divorce</code> jika ingin bercerai.",
            parse_mode="HTML"
        )

# ==========================================
# FAMILY COMMANDS
# ==========================================
async def cmd_create_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("❌ Format: <code>/create_family [nama keluarga]</code>", parse_mode="HTML")

    family_name = " ".join(context.args).strip()
    if not (3 <= len(family_name) <= 40):
        return await update.message.reply_text("❌ Nama keluarga harus 3-40 karakter.")
    if family_name.upper() in BLACKLISTED_FAMILY_NAMES:
        return await update.message.reply_text("🚫 Nama keluarga ini tidak diizinkan (reserved/impersonation).")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if await get_active_family_membership(db, user_id):
            return await update.message.reply_text("❌ Anda sudah tergabung dalam sebuah keluarga.")

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
            return await update.message.reply_text("❌ Nama keluarga sudah dipakai, pilih nama lain.")

        await update.message.reply_text(f"🏛️ <b>KELUARGA \"{family_name}\" DIDIRIKAN!</b>\n\nAnda menjadi kepala keluarga (<code>head</code>).", parse_mode="HTML")

async def cmd_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda belum tergabung dalam keluarga manapun. Gunakan <code>/create_family [nama]</code>.", parse_mode="HTML")

        family_id, relation_type, loyalty_score = membership
        async with db.execute("SELECT family_name, head_user_id, family_vault_balance, tax_rate_percent, is_locked FROM families WHERE family_id = ?", (family_id,)) as cursor:
            fam = await cursor.fetchone()
        family_name, head_id, vault_balance, tax_rate, is_locked = fam

        async with db.execute(
            "SELECT user_id, relation_type, loyalty_score FROM family_members WHERE family_id = ? AND is_active = 1 ORDER BY relation_type",
            (family_id,)
        ) as cursor:
            members = await cursor.fetchall()

        lines = [f"🏛️ <b>KELUARGA {family_name.upper()}</b>{' 🔒' if is_locked else ''}\n"]
        head_name = await get_username(db, head_id)
        lines.append(f"👑 Kepala Keluarga: @{head_name} (<code>{head_id}</code>)")
        lines.append(f"💰 Vault Keluarga: <b>{vault_balance:,} Koin</b>")
        lines.append(f"📊 Pajak Keluarga: <b>{tax_rate}%</b>")
        lines.append(f"\n<b>Anggota ({len(members)}):</b>")
        for m_id, m_rel, m_loyalty in members:
            m_name = await get_username(db, m_id)
            lines.append(f"• <code>{m_id}</code> (@{m_name}) — {m_rel} — Loyalty: {m_loyalty}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def _add_child(update: Update, context: ContextTypes.DEFAULT_TYPE, relation_type: str):
    user_id = update.effective_user.id
    child_id = parse_target_id(context)
    if child_id is None:
        cmd = "add_kandung" if relation_type == "biological" else "add_adopt"
        return await update.message.reply_text(f"❌ Format: <code>/{cmd} [user_id]</code>", parse_mode="HTML")
    if child_id == user_id:
        return await update.message.reply_text("❌ Tidak bisa menjadikan diri sendiri sebagai anak.")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, child_id):
            return await update.message.reply_text(
                f"❌ <b>User {child_id} belum terdaftar</b>\n\n"
                f"Minta mereka untuk mendaftar terlebih dahulu dengan <code>/register</code>.",
                parse_mode="HTML"
            )

        if await is_ancestor(db, child_id, user_id):
            return await update.message.reply_text("🚫 Tidak bisa: target adalah leluhur Anda (akan membuat relasi sirkular).")

        async with db.execute(
            "SELECT 1 FROM parent_child_relations WHERE parent_id = ? AND child_id = ? AND is_active = 1",
            (user_id, child_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi ini sudah tercatat.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO parent_child_relations (parent_id, child_id, relation_type, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (user_id, child_id, relation_type, now_epoch, user_id)
        )
        await db.commit()

        label = "anak kandung" if relation_type == "biological" else "anak angkat"
        child_name = await get_username(db, child_id)
        await update.message.reply_text(f"👶 @{child_name} (<code>{child_id}</code>) resmi tercatat sebagai <b>{label}</b> dari <code>{user_id}</code>.", parse_mode="HTML")

async def cmd_add_kandung(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_child(update, context, "biological")

async def cmd_add_adopt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_child(update, context, "adopted")

async def cmd_disown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    child_id = parse_target_id(context)
    if child_id is None:
        return await update.message.reply_text("❌ Format: <code>/disown [user_id]</code>", parse_mode="HTML")

    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Tidak disebutkan"
    async with get_db_connection() as db:
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
            return await update.message.reply_text("❌ Tidak ditemukan relasi anak aktif dengan user tersebut.")

        await update.message.reply_text(f"⚔️ <code>{child_id}</code> telah <b>didisown</b> dari keluarga <code>{user_id}</code>.\nAlasan: {reason}", parse_mode="HTML")

async def cmd_leave_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda tidak tergabung dalam keluarga manapun.")

        family_id, relation_type, loyalty_score = membership
        if relation_type == "head":
            return await update.message.reply_text("🚫 Kepala keluarga tidak bisa keluar begitu saja. Hubungi admin untuk transfer kepemimpinan.")

        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'voluntary' WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        await update.message.reply_text("🚪 Anda telah keluar dari keluarga secara sukarela.")

async def cmd_betray(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda tidak tergabung dalam keluarga manapun.")

        family_id, relation_type, loyalty_score = membership
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'betrayed', loyalty_score = 0 WHERE family_id = ? AND user_id = ?",
            (now_epoch, family_id, user_id)
        )
        await db.commit()
        await update.message.reply_text(
            "🗡️ <b>PENGKHIANATAN TERCATAT.</b>\n\nAnda keluar dari keluarga dengan status <i>betrayed</i>. Loyalty direset ke 0 secara permanen di riwayat ini.",
            parse_mode="HTML"
        )

async def cmd_loyalty_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, target_id):
            return await update.message.reply_text(
                f"❌ <b>User {target_id} belum terdaftar</b>\n\n"
                f"Minta mereka mendaftar terlebih dahulu dengan <code>/register</code>.", 
                parse_mode="HTML"
            )

        membership = await get_active_family_membership(db, target_id)
        if not membership:
            return await update.message.reply_text(f"❌ <code>{target_id}</code> tidak tergabung dalam keluarga manapun.", parse_mode="HTML")
        family_id, relation_type, loyalty_score = membership
        await update.message.reply_text(f"🏆 Loyalty <code>{target_id}</code>: <b>{loyalty_score}/100</b> ({relation_type})", parse_mode="HTML")

async def cmd_family_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        membership = await get_active_family_membership(db, user_id)
        if not membership:
            return await update.message.reply_text("❌ Anda tidak tergabung dalam keluarga manapun.")
        family_id = membership[0]

        async with db.execute(
            """SELECT user_id, relation_type, left_reason, left_at FROM family_members
               WHERE family_id = ? AND is_active = 0 ORDER BY left_at DESC LIMIT 10""",
            (family_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return await update.message.reply_text("📜 Belum ada riwayat anggota yang keluar dari keluarga ini.")

        lines = ["📜 <b>RIWAYAT KELUARGA (10 terakhir)</b>\n"]
        for m_id, rel, reason, left_at in rows:
            left_date = datetime.fromtimestamp(left_at, WIB).strftime("%d %b %Y") if left_at else "-"
            m_name = await get_username(db, m_id)
            lines.append(f"• @{m_name} (<code>{m_id}</code>) ({rel}) — {reason} — {left_date}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_add_sibling(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sibling_id = parse_target_id(context)
    if sibling_id is None:
        return await update.message.reply_text("❌ Format: <code>/add_sibling [user_id] [biological|adopted]</code>", parse_mode="HTML")
    if sibling_id == user_id:
        return await update.message.reply_text("❌ Tidak bisa menjadikan diri sendiri sebagai saudara.")

    sibling_type = "biological"
    if len(context.args) > 1 and context.args[1].lower() in ("biological", "adopted"):
        sibling_type = context.args[1].lower()

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, sibling_id):
            return await update.message.reply_text(
                f"❌ <b>User {sibling_id} belum terdaftar</b>\n\n"
                f"Minta mereka untuk mendaftar terlebih dahulu dengan <code>/register</code>.",
                parse_mode="HTML"
            )

        if await is_ancestor(db, sibling_id, user_id) or await is_ancestor(db, user_id, sibling_id):
            return await update.message.reply_text("🚫 Tidak bisa: target adalah orang tua/anak Anda, bukan saudara.")

        a_id, b_id = min(user_id, sibling_id), max(user_id, sibling_id)
        async with db.execute(
            "SELECT 1 FROM sibling_relations WHERE user_a_id = ? AND user_b_id = ? AND is_active = 1",
            (a_id, b_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi saudara ini sudah tercatat.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO sibling_relations (user_a_id, user_b_id, sibling_type, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (a_id, b_id, sibling_type, now_epoch, user_id)
        )
        await db.commit()

        label = "saudara kandung" if sibling_type == "biological" else "saudara angkat"
        sibling_name = await get_username(db, sibling_id)
        await update.message.reply_text(f"👫 @{sibling_name} (<code>{sibling_id}</code>) resmi tercatat sebagai <b>{label}</b> dari <code>{user_id}</code>.", parse_mode="HTML")

async def cmd_siblings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context) or user_id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            """SELECT user_a_id, user_b_id, sibling_type FROM sibling_relations
               WHERE (user_a_id = ? OR user_b_id = ?) AND is_active = 1""",
            (target_id, target_id)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return await update.message.reply_text(f"👫 <code>{target_id}</code> belum punya saudara tercatat.", parse_mode="HTML")

        lines = [f"👫 <b>SAUDARA DARI <code>{target_id}</code></b>\n"]
        for a_id, b_id, s_type in rows:
            other_id = b_id if a_id == target_id else a_id
            other_name = await get_username(db, other_id)
            label = "kandung" if s_type == "biological" else "angkat"
            lines.append(f"• @{other_name} (<code>{other_id}</code>) ({label})")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_godparent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    godparent_id = parse_target_id(context)
    if godparent_id is None:
        return await update.message.reply_text("❌ Format: <code>/godparent [user_id]</code> (menunjuk user_id sebagai godparent Anda)", parse_mode="HTML")
    if godparent_id == user_id:
        return await update.message.reply_text("❌ Tidak bisa menjadi godparent diri sendiri.")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, godparent_id):
            return await update.message.reply_text(
                f"❌ <b>User {godparent_id} belum terdaftar</b>\n\n"
                f"Minta mereka untuk mendaftar terlebih dahulu dengan <code>/register</code>.",
                parse_mode="HTML"
            )

        async with db.execute(
            "SELECT 1 FROM godparent_relations WHERE godparent_id = ? AND godchild_id = ? AND is_active = 1",
            (godparent_id, user_id)
        ) as cursor:
            if await cursor.fetchone():
                return await update.message.reply_text("❌ Relasi godparent ini sudah tercatat.")

        now_epoch = int(time.time())
        await db.execute(
            """INSERT INTO godparent_relations (godparent_id, godchild_id, registered_at, registered_by_id, is_active)
               VALUES (?, ?, ?, ?, 1)""",
            (godparent_id, user_id, now_epoch, user_id)
        )
        await db.commit()

        gp_name = await get_username(db, godparent_id)
        await update.message.reply_text(
            f"🕯️ @{gp_name} (<code>{godparent_id}</code>) resmi ditunjuk sebagai <b>godparent</b> dari <code>{user_id}</code>.\n"
            f"<i>(Catatan: godparent bukan ahli waris otomatis — kalau mau, atur manual lewat <code>/will</code>.)</i>",
            parse_mode="HTML"
        )

async def cmd_revoke_godparent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    godparent_id = parse_target_id(context)
    if godparent_id is None:
        return await update.message.reply_text("❌ Format: <code>/revoke_godparent [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
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
            return await update.message.reply_text("❌ Tidak ditemukan relasi godparent aktif dengan user tersebut.")
        await update.message.reply_text(f"🕯️ Status godparent <code>{godparent_id}</code> untuk Anda telah dicabut.", parse_mode="HTML")

async def cmd_my_godchildren(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute(
            "SELECT godchild_id FROM godparent_relations WHERE godparent_id = ? AND is_active = 1",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return await update.message.reply_text("🕯️ Anda belum menjadi godparent siapapun.")

        lines = ["🕯️ <b>GODCHILDREN ANDA</b>\n"]
        for r in rows:
            gc_name = await get_username(db, r[0])
            lines.append(f"• @{gc_name} (<code>{r[0]}</code>)")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_in_laws(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        marriage = await get_active_marriage(db, user_id)
        if not marriage:
            return await update.message.reply_text("❌ Anda belum menikah, jadi belum punya in-laws.")

        marriage_id, cert_number, user_a, user_b, married_at = marriage
        spouse_id = user_b if user_id == user_a else user_a
        spouse_name = await get_username(db, spouse_id)

        spouse_family = await get_active_family_membership(db, spouse_id)
        own_family = await get_active_family_membership(db, user_id)

        lines = [f"👪 <b>IN-LAWS (KELUARGA PASANGAN @{spouse_name})</b>\n"]

        if own_family and spouse_family and own_family[0] == spouse_family[0]:
            lines.append("Anda & pasangan sudah berada dalam satu keluarga besar yang sama.")
        elif not spouse_family:
            lines.append("Pasangan Anda belum tergabung dalam keluarga manapun.")
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
                lines.append("\n<b>Ipar/anggota lain:</b>")
                for m_id, m_rel in members:
                    m_name = await get_username(db, m_id)
                    lines.append(f"• @{m_name} (<code>{m_id}</code>) ({m_rel})")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ==========================================
# INHERITANCE / WILL COMMANDS
# ==========================================
async def cmd_will(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        return await update.message.reply_text(
            "📜 <b>Format Surat Wasiat:</b>\n\n"
            "<code>/will [user_id]:[persen] [user_id]:[persen] ...</code>\n\n"
            "Contoh:\n<code>/will 123456789:50 987654321:30</code>\n\n"
            "<i>Sisa persentase akan otomatis menjadi milik negara/default heir.</i>",
            parse_mode="HTML"
        )

    beneficiaries = []
    total_percent = 0.0
    for arg in context.args:
        if ":" not in arg:
            return await update.message.reply_text(
                f"❌ <b>Format tidak valid:</b> <code>{arg}</code>\n\n"
                f"Gunakan format <code>[user_id]:[persen]</code>\n\n"
                f"Contoh: <code>123456789:50.5</code>",
                parse_mode="HTML"
            )
        b_id_str, pct_str = arg.split(":", 1)
        if not b_id_str.isdigit():
            return await update.message.reply_text(
                f"❌ <b>Format tidak valid:</b> <code>{arg}</code>\n\n"
                f"Gunakan format <code>[user_id]:[persen]</code>\n\n"
                f"Contoh: <code>123456789:50.5</code>",
                parse_mode="HTML"
            )
        try:
            pct = float(pct_str)
        except ValueError:
            return await update.message.reply_text(
                f"❌ <b>Format tidak valid:</b> <code>{arg}</code>\n\n"
                f"Gunakan format <code>[user_id]:[persen]</code>\n\n"
                f"Contoh: <code>123456789:50.5</code>",
                parse_mode="HTML"
            )
        b_id = int(b_id_str)
        if b_id == user_id:
            return await update.message.reply_text(
                f"❌ <b>Tidak bisa mengalokasikan warisan untuk diri sendiri!</b> ({user_id})\n\n"
                f"Pilih beneficiary lain yang akan menerima warisan Anda.",
                parse_mode="HTML"
            )
        if pct <= 0:
            return await update.message.reply_text("❌ Persentase harus lebih dari 0.")
        beneficiaries.append((b_id, pct))
        total_percent += pct

    if total_percent > 100:
        return await update.message.reply_text(
            f"❌ <b>Total persentase {total_percent:.1f}% melebihi 100%.</b>\n\n"
            f"Silakan kurangi alokasi untuk beberapa beneficiary.\n\n"
            f"Contoh yang valid:\n<code>/will 123456789:50 987654321:30</code> (Total 80%)",
            parse_mode="HTML"
        )

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        unregistered = []
        for b_id, _ in beneficiaries:
            if not await user_exists(db, b_id):
                unregistered.append(str(b_id))

        if unregistered:
            unregistered_str = "\n   ".join(unregistered)
            return await update.message.reply_text(
                f"❌ <b>Beneficiary belum terdaftar:</b>\n   {unregistered_str}\n\n"
                f"Minta mereka mendaftar terlebih dahulu dengan <code>/register</code>.",
                parse_mode="HTML"
            )

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

        lines = ["📜 <b>SURAT WASIAT DIPERBARUI ✅</b>\n"]
        for b_id, pct in beneficiaries:
            b_name = await get_username(db, b_id)
            lines.append(f"• @{b_name} (<code>{b_id}</code>) — {pct}%")
        
        remaining_pct = 100 - total_percent
        if remaining_pct > 0:
            lines.append(f"\n⚠️ Sisa: {remaining_pct:.1f}% (tidak dialokasikan)")
        lines.append("\n💾 Wasiat disimpan dan siap dieksekusi dengan <code>/retire</code>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_appoint_heir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    heir_id = parse_target_id(context)
    if heir_id is None:
        return await update.message.reply_text(
            "⚰️ <b>Format:</b> <code>/appoint_heir [user_id]</code>\n\n"
            "Ini adalah shortcut untuk menunjuk 1 ahli waris dengan 100% alokasi.\n\n"
            "Contoh: <code>/appoint_heir 123456789</code>",
            parse_mode="HTML"
        )
    if heir_id == user_id:
        return await update.message.reply_text("❌ Tidak bisa mengangkat diri sendiri sebagai ahli waris.")

    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        if not await user_exists(db, heir_id):
            return await update.message.reply_text(
                f"❌ <b>User {heir_id} belum terdaftar</b>\n\n"
                f"Minta mereka mendaftar dengan <code>/register</code> terlebih dahulu.",
                parse_mode="HTML"
            )

    context.args = [f"{heir_id}:100"]
    await cmd_will(update, context)

async def cmd_will_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status, updated_at, executed_at FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            return await update.message.reply_text(
                "📜 <b>Anda belum memiliki surat wasiat</b>\n\n"
                "Buat dengan: <code>/will [user_id]:[persen] ...</code>\n"
                "atau shortcut: <code>/appoint_heir [user_id]</code>",
                parse_mode="HTML"
            )

        will_id, status, updated_at, executed_at = will
        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return await update.message.reply_text(
                f"📜 <b>Surat Wasiat Anda KOSONG</b>\n\n"
                f"Status: {status.upper()}\n\n"
                f"Tambahkan beneficiary dengan <code>/will [user_id]:[persen]</code>",
                parse_mode="HTML"
            )

        status_label = "EXECUTED" if status == "executed" else "ACTIVE"
        lines = [f"📜 <b>SURAT WASIAT — Status: {status_label}</b>\n"]
        for b_id, pct in rows:
            b_name = await get_username(db, b_id)
            lines.append(f"• @{b_name} (<code>{b_id}</code>) — {pct}%")
            
        if executed_at:
            exec_date = datetime.fromtimestamp(executed_at, WIB).strftime("%d %B %Y, %H:%M WIB")
            lines.append(f"\n✅ <b>SUDAH DIEKSEKUSI pada:</b> {exec_date}")
        else:
            lines.append("\n💾 Gunakan <code>/retire</code> untuk mengeksekusi wasiat ini")
            
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_retire(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        if not await ensure_user_registered(update, db, user_id):
            return

        async with db.execute("SELECT will_id, status FROM wills WHERE owner_id = ?", (user_id,)) as cursor:
            will = await cursor.fetchone()
        if not will:
            return await update.message.reply_text(
                "❌ <b>Anda belum punya surat wasiat</b>\n\n"
                "Buat dulu dengan <code>/appoint_heir [user_id]</code> atau <code>/will [user_id]:[persen] ...</code>",
                parse_mode="HTML"
            )

        will_id, status = will
        if status == "executed":
            return await update.message.reply_text("❌ Surat wasiat ini sudah pernah dieksekusi sebelumnya.")

        async with db.execute("SELECT beneficiary_id, percent FROM will_beneficiaries WHERE will_id = ?", (will_id,)) as cursor:
            beneficiaries = await cursor.fetchall()
        if not beneficiaries:
            return await update.message.reply_text(
                "❌ <b>Surat wasiat kosong</b>\n\n"
                "Tidak ada beneficiary yang dialokasikan. Tambahkan dengan <code>/will [user_id]:[persen]</code>",
                parse_mode="HTML"
            )

        total_koin = await get_koin(db, user_id)
        if total_koin <= 0:
            return await update.message.reply_text(
                "❌ <b>Tidak ada koin untuk diwariskan</b>\n\n"
                f"Saldo Anda: <b>0 Koin</b>\n\n"
                "Anda perlu memiliki koin sebelum bisa eksekusi wasiat.",
                parse_mode="HTML"
            )

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
        await db.execute("UPDATE wills SET status = 'executed', executed_at = ? WHERE will_id = ?", (now_epoch, will_id))
        await db.commit()

        text = (
            "⚰️ <b>WASIAT BERHASIL DIEKSEKUSI (RETIRE)</b>\n\n"
            + "\n".join(distributed_lines)
            + f"\n\n💰 Total diwariskan: <b>{total_distributed:,} Koin</b>"
            + f"\n💾 Sisa di rekening Anda: <b>{remaining:,} Koin</b>"
            + "\n\n✅ <i>Proses retire selesai. Para ahli waris telah menerima warisan mereka.</i>"
        )
        await update.message.reply_text(text, parse_mode="HTML")

# ==========================================
# ADMIN COMMANDS
# ==========================================
async def cmd_lineage_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 <b>AKSES DITOLAK:</b> Anda tidak memiliki otoritas Administrator.", parse_mode="HTML")

        text = (
            f"🛠️ <b>LINEAGE ADMIN PANEL</b>\n\n"
            f"Level Otoritas Anda: <b>Tier {tier}</b>\n\n"
            f"<b>Fitur Admin:</b>\n"
            f"• <code>/lock_family [family_id] [alasan]</code> (Tier 2+)\n"
            f"• <code>/unlock_family [family_id]</code> (Tier 2+)\n"
            f"• <code>/force_divorce [user_id]</code> (Tier 2+)\n\n"
            f"<b>Aksi Berat (butuh 2 admin berbeda):</b>\n"
            f"• <code>/excommunicate [user_id] [alasan]</code> — submit request (Tier 3+)\n"
            f"• <code>/approve_action [action_id]</code> — approve & eksekusi (Tier 3+, admin BEDA dari requester)\n\n"
            f"<b>Cheat:</b>\n"
            f"• <code>/cheat_set_loyalty [user_id] [score]</code> (Tier 1+)"
        )
        await update.message.reply_text(text, parse_mode="HTML")

async def cmd_lock_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("❌ Format: <code>/lock_family [family_id] [alasan]</code>", parse_mode="HTML")
    family_id = int(context.args[0])
    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Investigasi Admin"

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")
        cursor = await db.execute("UPDATE families SET is_locked = 1, lock_reason = ? WHERE family_id = ?", (reason, family_id))
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Family ID tidak ditemukan.")
        await update.message.reply_text(f"🔒 Keluarga <code>{family_id}</code> dikunci.\nAlasan: {reason}", parse_mode="HTML")

async def cmd_unlock_family(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("❌ Format: <code>/unlock_family [family_id]</code>", parse_mode="HTML")
    family_id = int(context.args[0])

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")
        cursor = await db.execute("UPDATE families SET is_locked = 0, lock_reason = NULL WHERE family_id = ?", (family_id,))
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Family ID tidak ditemukan.")
        await update.message.reply_text(f"🔓 Keluarga <code>{family_id}</code> dibuka kembali.", parse_mode="HTML")

async def cmd_force_divorce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text("❌ Format: <code>/force_divorce [user_id]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        marriage = await get_active_marriage(db, target_id)
        if not marriage:
            return await update.message.reply_text("❌ Target tidak sedang menikah.")

        marriage_id = marriage[0]
        now_epoch = int(time.time())
        await db.execute(
            "UPDATE marriages SET status = 'divorced', divorced_at = ?, divorce_reason = 'force_admin' WHERE marriage_id = ?",
            (now_epoch, marriage_id)
        )
        await db.commit()
        await update.message.reply_text(f"⚖️ <b>FORCE DIVORCE</b> dieksekusi oleh Admin pada pernikahan <code>{marriage[1]}</code>.", parse_mode="HTML")

async def cmd_excommunicate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = parse_target_id(context)
    if target_id is None:
        return await update.message.reply_text("❌ Format: <code>/excommunicate [user_id] [alasan]</code>", parse_mode="HTML")
    reason = " ".join(context.args[1:]).strip() if len(context.args) > 1 else "Tidak disebutkan"

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 3:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 3+.")

        now_epoch = int(time.time())
        cursor = await db.execute(
            """INSERT INTO lineage_admin_actions (action_type, target_id, note, requested_by, status, created_at)
               VALUES ('excommunicate', ?, ?, ?, 'pending', ?)""",
            (target_id, reason, user_id, now_epoch)
        )
        action_id = cursor.lastrowid
        await db.commit()

        await update.message.reply_text(
            f"📋 <b>REQUEST EXCOMMUNICATE DIBUAT</b> (ID: <code>{action_id}</code>)\n\n"
            f"Target: <code>{target_id}</code>\nAlasan: {reason}\n\n"
            f"⚠️ Butuh approval dari Admin Tier 3+ LAIN via <code>/approve_action {action_id}</code> sebelum dieksekusi.",
            parse_mode="HTML"
        )

async def cmd_approve_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("❌ Format: <code>/approve_action [action_id]</code>", parse_mode="HTML")
    action_id = int(context.args[0])

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 3:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 3+.")

        async with db.execute(
            "SELECT action_type, target_id, note, requested_by, status FROM lineage_admin_actions WHERE action_id = ?",
            (action_id,)
        ) as cursor:
            action = await cursor.fetchone()

        if not action:
            return await update.message.reply_text("❌ Action ID tidak ditemukan.")

        action_type, target_id, note, requested_by, status = action
        if status != "pending":
            return await update.message.reply_text(f"❌ Action ini sudah berstatus <code>{status}</code>, tidak bisa diproses ulang.", parse_mode="HTML")
        if requested_by == user_id:
            return await update.message.reply_text("🚫 Anda tidak bisa approve request Anda sendiri. Butuh admin Tier 3+ LAIN.")

        now_epoch = int(time.time())

        if action_type == "excommunicate":
            await db.execute(
                "UPDATE family_members SET is_active = 0, left_at = ?, left_reason = 'excommunicated' WHERE user_id = ? AND is_active = 1",
                (now_epoch, target_id)
            )

        await db.execute(
            "UPDATE lineage_admin_actions SET status = 'executed', approved_by = ?, executed_at = ? WHERE action_id = ?",
            (user_id, now_epoch, action_id)
        )
        await db.commit()

        await update.message.reply_text(
            f"✅ <b>ACTION <code>{action_id}</code> DIAPPROVE & DIEKSEKUSI</b>\n\n"
            f"Tipe: {action_type}\nTarget: <code>{target_id}</code>\nRequested by: <code>{requested_by}</code>\nApproved by: <code>{user_id}</code>",
            parse_mode="HTML"
        )

async def cmd_cheat_set_loyalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].lstrip("-").isdigit():
        return await update.message.reply_text("❌ Format: <code>/cheat_set_loyalty [user_id] [score]</code>", parse_mode="HTML")
    target_id = int(context.args[0])
    score = max(0, min(100, int(context.args[1])))

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 <b>CHEAT DITOLAK:</b> Anda tidak memiliki akses Admin!", parse_mode="HTML")

        cursor = await db.execute(
            "UPDATE family_members SET loyalty_score = ? WHERE user_id = ? AND is_active = 1",
            (score, target_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return await update.message.reply_text("❌ Target tidak punya keanggotaan keluarga aktif.")

        await update.message.reply_text(f"🧪 <b>CHEAT:</b> Loyalty <code>{target_id}</code> diset ke <b>{score}</b>.", parse_mode="HTML")

# ==========================================
# START / HELP
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🧬 <b>WELCOME TO COSA NOSTRA LINEAGE BOT</b>\n\n"
        "💍 <b>Marriage:</b>\n"
        "/propose [user_id] — Lamar\n"
        "/accept_proposal [user_id] — Terima lamaran\n"
        "/reject_proposal [user_id] — Tolak lamaran\n"
        "/divorce — Cerai\n"
        "/marriage_status — Status pernikahan\n\n"
        "🏛️ <b>Family:</b>\n"
        "/create_family [nama] — Dirikan keluarga\n"
        "/family — Info keluarga\n"
        "/add_kandung [user_id] — Tambah anak kandung\n"
        "/add_adopt [user_id] — Tambah anak angkat\n"
        "/disown [user_id] — Disown anak\n"
        "/leave_family — Keluar sukarela\n"
        "/betray — Khianati keluarga\n"
        "/loyalty_check [user_id] — Cek loyalty\n"
        "/family_history — Riwayat keluarga\n"
        "/add_sibling [user_id] — Tambah saudara\n"
        "/siblings [user_id] — Lihat daftar saudara\n"
        "/godparent [user_id] — Tunjuk godparent\n"
        "/revoke_godparent [user_id] — Cabut godparent\n"
        "/my_godchildren — Lihat godchildren Anda\n"
        "/in_laws — Lihat keluarga pasangan\n\n"
        "⚰️ <b>Inheritance:</b>\n"
        "/will [id:persen ...] — Atur wasiat\n"
        "/appoint_heir [user_id] — Ahli waris tunggal\n"
        "/will_status — Cek wasiat\n"
        "/retire — Eksekusi wasiat\n\n"
        "🛠️ <b>ADMINISTRATOR:</b> /lineage_admin_panel"
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start))

    # Marriage
    app.add_handler(CommandHandler("propose", cmd_propose))
    app.add_handler(CommandHandler("accept_proposal", cmd_accept_proposal))
    app.add_handler(CommandHandler("reject_proposal", cmd_reject_proposal))
    app.add_handler(CommandHandler("divorce", cmd_divorce))
    app.add_handler(CommandHandler("marriage_status", cmd_marriage_status))

    # Family
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

    # Inheritance
    app.add_handler(CommandHandler("will", cmd_will))
    app.add_handler(CommandHandler("appoint_heir", cmd_appoint_heir))
    app.add_handler(CommandHandler("will_status", cmd_will_status))
    app.add_handler(CommandHandler("retire", cmd_retire))

    # Admin
    app.add_handler(CommandHandler("lineage_admin_panel", cmd_lineage_admin_panel))
    app.add_handler(CommandHandler("lock_family", cmd_lock_family))
    app.add_handler(CommandHandler("unlock_family", cmd_unlock_family))
    app.add_handler(CommandHandler("force_divorce", cmd_force_divorce))
    app.add_handler(CommandHandler("excommunicate", cmd_excommunicate))
    app.add_handler(CommandHandler("approve_action", cmd_approve_action))
    app.add_handler(CommandHandler("cheat_set_loyalty", cmd_cheat_set_loyalty))

    return app

def main():
    app = build_app()
    print("🧬 Telegram Cosa Nostra Lineage Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
