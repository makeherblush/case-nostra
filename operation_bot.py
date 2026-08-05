import os
import aiosqlite
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
RAW_TOKEN = os.getenv("TELEGRAM_OPERATIONS_BOT_TOKEN")
TOKEN = RAW_TOKEN.strip()

# Path Absolut agar kedua bot membaca file fisik database yang sama persis
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "cosa_nostra.db")
WIB = timezone(timedelta(hours=7)) # UTC+7

# ==========================================
# HELPER KONEKSI DATABASE (WAL MODE ENABLER)
# ==========================================
@asynccontextmanager
async def get_db_connection():
    """
    Membuka koneksi SQLite dengan konfigurasi WAL (Write-Ahead Logging) 
    menggunakan async context manager agar multi-process/bot aman membaca & menulis.
    """
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
USER_COLUMNS = "user_id, username, koin, bank_balance, bank_loan, vitality, gelar_tier, heat, respect, admin_tier, jailed_until, bounty, crew_id, last_work, last_daily, job_active, job_finish_time, last_business_collect"

async def sync_gelar_from_assets(db, user_id: int) -> str:
    """
    Mengecek inventaris user di tabel assets secara otomatis.
    Jika user memiliki item gelar (seperti CSN-GLR-...), update kolom gelar_tier di tabel users.
    """
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
        pass # Jika tabel assets belum ada, fallback ke gelar_tier yang tersimpan
    
    async with db.execute("SELECT gelar_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        res = await cursor.fetchone()
        return res[0] if res else "G0"

async def get_or_create_user(db, user_id: int, username: str):
    """Fungsi standar untuk mengambil atau membuat data user secara konsisten berdasarkan user_id."""
    async with db.execute(f"SELECT {USER_COLUMNS} FROM users WHERE user_id = ?", (user_id,)) as cursor:
        user = await cursor.fetchone()
        
    if user:
        if user[1] != username:
            await db.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            await db.commit()
            
        # Sinkronkan Gelar dari Assets secara otomatis
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
# MATRIX JOB & CRIME DATASTRUCTURE
# ==========================================
JOBS = {
    # TIER 1: Made Man (G1)
    "debt": {"name": "Debt Collector", "tier": "G1", "dur": 3600, "vit": 15, "min": 400, "max": 800, "desc": "Kumpulin utang dari debitur lokal"},
    "smuggle_s": {"name": "Small Smuggling", "tier": "G1", "dur": 7200, "vit": 20, "min": 1000, "max": 2000, "desc": "Selundupkan barang kecil"},
    "pickpocket": {"name": "Pickpocket", "tier": "G1", "dur": 3600, "vit": 10, "min": 500, "max": 1200, "desc": "Curi dompet warga"},
    "muscle": {"name": "Street Muscle", "tier": "G1", "dur": 5400, "vit": 18, "min": 600, "max": 1000, "desc": "Intimidasi target di jalanan"},
    "drug_run": {"name": "Drug Runner", "tier": "G1", "dur": 10800, "vit": 25, "min": 1500, "max": 3000, "desc": "Antar paket narkoba ke distributor"},

    # TIER 2: Enforcer Primus (G2)
    "blackmail": {"name": "Blackmail Specialist", "tier": "G2", "dur": 7200, "vit": 20, "min": 2000, "max": 4000, "desc": "Ancam & peras target rentan"},
    "protection": {"name": "Protected Territory", "tier": "G2", "dur": 14400, "vit": 30, "min": 2500, "max": 5000, "desc": "Pungut uang perlindungan pertokoan"},
    "sabotage": {"name": "Sabotage Operation", "tier": "G2", "dur": 10800, "vit": 25, "min": 3000, "max": 6000, "desc": "Rusak fasilitas & aset musuh"},
    "interrogate": {"name": "Interrogation", "tier": "G2", "dur": 7200, "vit": 22, "min": 1500, "max": 3500, "desc": "Interogasi paksa target"},
    "arms_trade": {"name": "Arms Trading", "tier": "G2", "dur": 10800, "vit": 28, "min": 4000, "max": 7000, "desc": "Jual beli senjata gelap"},

    # TIER 3: Capo Regime (G3)
    "conquer": {"name": "Territorial Conquest", "tier": "G3", "dur": 18000, "vit": 35, "min": 5000, "max": 10000, "desc": "Ambil alih wilayah baru"},
    "contract": {"name": "Hit Job Contract", "tier": "G3", "dur": 14400, "vit": 30, "min": 6000, "max": 12000, "desc": "Eksekusi kontrak pembunuhan"},
    "heist_plan": {"name": "Heist Planning", "tier": "G3", "dur": 21600, "vit": 40, "min": 7000, "max": 15000, "desc": "Rencanakan perampokan besar"},

    # TIER 4: Underboss Executive (G4)
    "corrupt": {"name": "Government Corruption", "tier": "G4", "dur": 18000, "vit": 35, "min": 10000, "max": 20000, "desc": "Suap pejabat publik"},
    "bank_heist": {"name": "Major Bank Heist", "tier": "G4", "dur": 25200, "vit": 50, "min": 15000, "max": 30000, "desc": "Rampok bank nasional"},

    # TIER 5: Grand Consigliere (G5)
    "policy": {"name": "National Policy Control", "tier": "G5", "dur": 21600, "vit": 40, "min": 20000, "max": 40000, "desc": "Kendalikan kebijakan hukum"},
    "traffic": {"name": "International Trafficking", "tier": "G5", "dur": 28800, "vit": 50, "min": 25000, "max": 50000, "desc": "Penyelundupan internasional"},

    # TIER 6: Caporegime Supremo (G6)
    "topple": {"name": "Topple Government", "tier": "G6", "dur": 43200, "vit": 60, "min": 60000, "max": 120000, "desc": "Gulingkan rezim pemerintahan"},

    # TIER 7: Don / Donna Famiglia (G7)
    "ultimate": {"name": "Ultimate World Domination", "tier": "G7", "dur": 108000, "vit": 80, "min": 200000, "max": 400000, "desc": "Kuasai tatanan dunia baru"}
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
                last_business_collect INTEGER DEFAULT 0
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
    MY_PERMANENT_OWNER_ID = 8396793986  
    if user_id == MY_PERMANENT_OWNER_ID:
        return 4  

    async with db.execute("SELECT admin_tier FROM users WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

# ==========================================
# PUBLIC COMMAND HANDLERS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"

    async with get_db_connection() as db:
        await get_or_create_user(db, user_id, current_username)

    text = (
        "⚔️ WELCOME TO COSA NOSTRA OPERATIONS BOT\n\n"
        "Gunakan command berikut untuk memulai aksi:\n"
        "👤 /rekening - Status Profil & Vitality\n"
        "🔨 /work - Kerja Harian\n"
        "🎁 /daily - Klaim Bonus Harian\n"
        "💼 /job [type] - Misi Operasi Bertingkat\n"
        "🕵️ /crime [type] - Kejahatan Berisiko\n"
        "🎯 /hit [user_id] - Eksekusi Target\n"
        "💰 /bounty [user_id] [koin] - Pasang Kontrak\n"
        "🚔 /wanted - Cek Daftar Buronan\n"
        "🏴‍☠️ /crew - Manajemen Organisasi\n\n"
        "🛠️ ADMINISTRATOR: /admin_panel"
    )
    await update.message.reply_text(text)

async def cmd_rekening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
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

        text = (
            f"👤 *PROFIL ANGGOTA COSA NOSTRA*\n\n"
            f"Nama: *@{db_username}* (`{user_id}`)\n"
            f"Gelar Pangkat: *{gelar}*\n"
            f"Crew: *{crew_name}*\n"
            f"───────────────────\n"
            f"💵 Cash Tunai: *{koin:,} Koin*\n"
            f"🏦 Tabungan Bank: *{bank_balance:,} Koin*\n"
            f"⚡ Vitality: *{vitality}%*\n"
            f"🔥 Heat Level: *{heat}*\n"
            f"🏆 Respect: *{respect}*\n"
            f"🎯 Bounty: *{bounty:,} Koin*\n"
            f"Status Hukum: {jail_status}"
        )
        
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        
        if user[10] > now_epoch:
            return await update.message.reply_text("🔒 Anda sedang dalam sel penjara! Tidak dapat bekerja.")

        if user[5] < 20:
            return await update.message.reply_text("⚡ Vitality Anda terlalu rendah (<20%)! Beli makanan di Vault Bot (`/shop makanan`).")

        last_work = user[13]
        if now_epoch - last_work < 3600:
            rem = 3600 - (now_epoch - last_work)
            return await update.message.reply_text(f"⏳ Istirahat dulu! Bekerja lagi dalam **{rem//60}m {rem%60}s**.", parse_mode="Markdown")

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
            f"🔨 *KERJA SELESAI!*\n\n"
            f"Gaji Dasar: +{base_pay:,} Koin\n"
            f"Bonus Gelar ({gelar}): +{gelar_bonus:,} Koin\n"
            f"Total Diterima: *+{total_income:,} Koin*\n"
            f"Vitality Terkuras: -15% (Sisa: {new_vit}%)",
            parse_mode="Markdown"
        )

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        last_daily = user[14]

        if now_epoch - last_daily < 86400:
            rem = 86400 - (now_epoch - last_daily)
            return await update.message.reply_text(f"⏳ Klaim harian berikutnya dalam **{rem//3600}j {(rem%3600)//60}m**.", parse_mode="Markdown")

        reward = 2000
        await db.execute("UPDATE users SET koin = koin + ?, last_daily = ? WHERE user_id = ?", (reward, now_epoch, user_id))
        await db.commit()

        await update.message.reply_text(f"🎁 *BONUS HARIAN:* Anda mendapatkan *+{reward:,} Koin*!", parse_mode="Markdown")

async def cmd_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        active_job = user[15]
        job_finish = user[16]

        if user[10] > now_epoch:
            return await update.message.reply_text("🔒 Anda dalam penjara! Tidak dapat mengeksekusi misi.")

        if active_job and job_finish > 0:
            if now_epoch < job_finish:
                rem = job_finish - now_epoch
                return await update.message.reply_text(
                    f"⏳ Sedang menjalankan **{JOBS[active_job]['name']}**. Selesai dalam **{rem//3600}j {(rem%3600)//60}m {rem%60}s**.",
                    parse_mode="Markdown"
                )
            else:
                j_info = JOBS[active_job]
                reward = random.randint(j_info["min"], j_info["max"])
                await db.execute(
                    "UPDATE users SET koin = koin + ?, job_active = NULL, job_finish_time = 0 WHERE user_id = ?",
                    (reward, user_id)
                )
                await db.commit()
                return await update.message.reply_text(
                    f"🎉 *MISI BERHASIL!*\n\nAnda menyelesaikan **{j_info['name']}** dan mendapatkan *+{reward:,} Koin*!",
                    parse_mode="Markdown"
                )

        if not args:
            text = "💼 *DAFTAR MISI JOB TERSEDIA*\n\nGunakan `/job [job_code]` untuk memulai:\n\n"
            for code, j in JOBS.items():
                text += f"• `[{code}]` *{j['name']}* ({j['tier']}+)\n  Durasi: {j['dur']//3600}j | Hasil: {j['min']:,}-{j['max']:,} Koin\n"
            return await update.message.reply_text(text, parse_mode="Markdown")

        job_code = args[0].lower()
        if job_code not in JOBS:
            return await update.message.reply_text("❌ Kode Job tidak ditemukan!")

        j = JOBS[job_code]
        user_gelar = user[6]
        user_tier_num = int(user_gelar.replace("G", "")) if user_gelar.startswith("G") else 0
        req_tier_num = int(j["tier"].replace("G", ""))

        if user_tier_num < req_tier_num:
            return await update.message.reply_text(f"🔒 **AKSES DITOLAK:** Misi ini membutuhkan minimal gelar **{j['tier']}**!", parse_mode="Markdown")

        if user[5] < j["vit"]:
            return await update.message.reply_text(f"⚡ Vitality tidak cukup! Membutuhkan {j['vit']}% Vitality.")

        finish_time = now_epoch + j["dur"]
        new_vit = user[5] - j["vit"]

        await db.execute(
            "UPDATE users SET vitality = ?, job_active = ?, job_finish_time = ? WHERE user_id = ?",
            (new_vit, job_code, finish_time, user_id)
        )
        await db.commit()

        await update.message.reply_text(
            f"🚀 *MISI DIMULAI: {j['name']}*\n\n"
            f"Durasi: {j['dur']//3600} Jam\n"
            f"Proyeksi Hasil: {j['min']:,} - {j['max']:,} Koin\n"
            f"Ketik `/job` kembali setelah durasi selesai untuk mengambil hasil.",
            parse_mode="Markdown"
        )

async def cmd_crime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)

        if user[10] > now_epoch:
            return await update.message.reply_text("🔒 Anda sedang mendekam di sel penjara!")

        if not args:
            text = "🕵️ *DAFTAR AKSI KEJAHATAN*\n\nGunakan `/crime [crime_code]`:\n\n"
            for code, c in CRIMES.items():
                text += f"• `[{code}]` *{c['desc']}*\n  Hasil: {c['min']:,}-{c['max']:,} Koin | Risiko Tertangkap: {c['risk']}%\n"
            return await update.message.reply_text(text, parse_mode="Markdown")

        crime_code = args[0].lower()
        if crime_code not in CRIMES:
            return await update.message.reply_text("❌ Kode aksi kejahatan tidak valid.")

        c = CRIMES[crime_code]

        if random.randint(1, 100) <= c["risk"]:
            jail_until = now_epoch + c["jail"]
            fine = c["min"] // 2

            await db.execute(
                "UPDATE users SET koin = MAX(0, koin - ?), heat = heat + ?, jailed_until = ? WHERE user_id = ?",
                (fine, c["heat"], jail_until, user_id)
            )
            await db.commit()

            return await update.message.reply_text(
                f"🚨 *AKSI GAGAL! ANDA TERTANGKAP POLISI!*\n\n"
                f"Denda Disita: -{fine:,} Koin\n"
                f"Heat Bertambah: +{c['heat']}\n"
                f"Mendekam di Penjara: {c['jail']//3600} Jam",
                parse_mode="Markdown"
            )

        loot = random.randint(c["min"], c["max"])
        await db.execute(
            "UPDATE users SET koin = koin + ?, heat = heat + ?, respect = respect + 10 WHERE user_id = ?",
            (loot, c["heat"] // 2, user_id)
        )
        await db.commit()

        await update.message.reply_text(
            f"🎭 *AKSI KEJAHATAN SUKSES!*\n\n"
            f"Hasil Rampokan: *+{loot:,} Koin*\n"
            f"Respect Bertambah: +10\n"
            f"Heat Level: +{c['heat']//2}",
            parse_mode="Markdown"
        )

async def cmd_bounty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"

    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        return await update.message.reply_text("❌ Format: `/bounty [target_user_id] [jumlah_koin]`", parse_mode="Markdown")

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

    await update.message.reply_text(f"🎯 *KONTRAK DIPASANG:* Sayembara sebesar *{bounty_amount:,} Koin* terpasang untuk kepala ID `{target_id}`!", parse_mode="Markdown")

async def cmd_hit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    if not args or not args[0].isdigit():
        return await update.message.reply_text("❌ Format: `/hit [target_user_id]`", parse_mode="Markdown")

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
                f"☠️ *EKSEKUSI HIT BERHASIL!*\n\n"
                f"Target `{target_id}` berhasil dieksekusi!\n"
                f"Koin Dirampok: {stolen_cash:,} Koin\n"
                f"Bounty Diklaim: {target_bounty:,} Koin\n"
                f"Total Hasil: *+{total_prize:,} Koin*",
                parse_mode="Markdown"
            )
        else:
            jail_until = now_epoch + 7200
            await db.execute("UPDATE users SET jailed_until = ? WHERE user_id = ?", (jail_until, user_id))
            await db.commit()

            return await update.message.reply_text(f"❌ *HIT GAGAL!* Target meloloskan diri dan Anda dijebloskan ke penjara selama 2 jam.")

async def cmd_wanted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT user_id, username, bounty FROM users WHERE bounty > 0 ORDER BY bounty DESC LIMIT 10") as cursor:
            wanted_list = await cursor.fetchall()

        if not wanted_list:
            return await update.message.reply_text("🕊️ Saat ini tidak ada kontrak buronan aktif.")

        text = "🚔 *DAFTAR BURONAN COSA NOSTRA*\n\n"
        for idx, (u_id, name, b_val) in enumerate(wanted_list, 1):
            text += f"{idx}. *@{name}* (`{u_id}`) - 🎯 *{b_val:,} Koin*\n"

        text += "\nGunakan `/hit [user_id]` untuk mengeksekusi buronan."
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_crew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    current_username = update.effective_user.username or "TanpaUsername"
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        user = await get_or_create_user(db, user_id, current_username)
        user_crew_id = user[12]

        if not args:
            if user_crew_id == 0:
                text = (
                    "🏴‍☠️ *SISTEM ORGANISASI CREW*\n\n"
                    "Anda belum bergabung dengan crew apa pun.\n"
                    "• `/crew create [nama_crew]` - Buat Crew baru (Biaya: 50.000 Koin)\n"
                    "• `/crew join [nama_crew]` - Bergabung dengan Crew"
                )
            else:
                async with db.execute("SELECT crew_name, treasury, respect FROM crews WHERE crew_id = ?", (user_crew_id,)) as c_cur:
                    c_info = await c_cur.fetchone()
                text = (
                    f"🏴‍☠️ *CREW ANDA: {c_info[0]}*\n\n"
                    f"💰 Kas Treasury: *{c_info[1]:,} Koin*\n"
                    f"🏆 Crew Respect: *{c_info[2]}*\n\n"
                    f"Command:\n"
                    f"• `/crew donate [jumlah]` - Salurkan kas ke treasury"
                )
            return await update.message.reply_text(text, parse_mode="Markdown")

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

            return await update.message.reply_text(f"🏴‍☠️ **CREW BERHASIL DIBUAT:** Selamat mendirikan **{crew_name}**!", parse_mode="Markdown")

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

            return await update.message.reply_text(f"✅ Berhasil mendonasikan *{donate_amount:,} Koin* ke Treasury Crew.", parse_mode="Markdown")

# ==========================================
# ADMIN & CHEAT COMMANDS
# ==========================================
async def cmd_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier == 0:
            return await update.message.reply_text("🚫 **AKSES DITOLAK:** Anda tidak memiliki otoritas Administrator.")

        text = (
            f"🛠️ *OPERATIONS ADMIN & CHEAT PANEL*\n\n"
            f"Level Otoritas Anda: *Tier {tier}*\n\n"
            f"*Fitur Admin:*\n"
            f"• `/jail_user [user_id] [jam]`\n"
            f"• `/unjail_user [user_id]`\n"
            f"• `/clear_heat [user_id]`\n\n"
            f"*Fitur Cheat Admin:*\n"
            f"• `/cheat_godmode [target_id]`\n"
            f"• `/cheat_instant_work [target_id]`\n"
            f"• `/cheat_clear_bounty [target_id]`"
        )
        await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_cheat_godmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda tidak memiliki akses Admin!")

        await db.execute(
            "UPDATE users SET vitality = 100, heat = 0, jailed_until = 0 WHERE user_id = ?",
            (target_id,)
        )
        await db.commit()

        await update.message.reply_text(
            f"🧪 **GODMODE ACTIVATED!**\n\nTarget ID: `{target_id}`\n⚡ Vitality: **100%**\n🔥 Heat Level: **0**\n🔓 Status Hukum: **BEBAS PENJARA**",
            parse_mode="Markdown"
        )

async def cmd_cheat_instant_work(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda tidak memiliki akses Admin!")

        await db.execute(
            "UPDATE users SET last_work = 0, last_daily = 0, job_finish_time = 1 WHERE user_id = ?",
            (target_id,)
        )
        await db.commit()

        await update.message.reply_text(
            f"🧪 **INSTANT RESET ACTIVATED!**\n\nTarget ID: `{target_id}`\n• Cooldown `/work` & `/daily` di-reset ke 0!\n• Job aktif siap diklaim sekarang (`/job`).",
            parse_mode="Markdown"
        )

async def cmd_cheat_clear_bounty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    target_id = int(context.args[0]) if context.args and context.args[0].isdigit() else user_id

    async with get_db_connection() as db:
        if await check_admin_tier(db, user_id) < 1:
            return await update.message.reply_text("🚫 **CHEAT DITOLAK:** Anda tidak memiliki akses Admin!")

        await db.execute("UPDATE users SET bounty = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🧪 **ADMIN CHEAT:** Bounty pada User ID `{target_id}` telah dibersihkan!", parse_mode="Markdown")

async def cmd_jail_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 1+.")

        if len(context.args) < 2:
            return await update.message.reply_text("❌ Format: `/jail_user [target_id] [jam]`")

        target_id = int(context.args[0])
        hours = int(context.args[1])
        jail_time = int(time.time()) + (hours * 3600)

        await db.execute("UPDATE users SET jailed_until = ? WHERE user_id = ?", (jail_time, target_id))
        await db.commit()

        await update.message.reply_text(f"🔒 Target `{target_id}` berhasil dipenjara selama **{hours} jam**.", parse_mode="Markdown")

async def cmd_unjail_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 2:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 2+.")

        if not context.args:
            return await update.message.reply_text("❌ Format: `/unjail_user [target_id]`")

        target_id = int(context.args[0])
        await db.execute("UPDATE users SET jailed_until = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🔓 Target `{target_id}` telah dibebaskan dari penjara.", parse_mode="Markdown")

async def cmd_clear_heat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 3:
            return await update.message.reply_text("🚫 Butuh akses Admin Tier 3+.")

        if not context.args:
            return await update.message.reply_text("❌ Format: `/clear_heat [target_id]`")

        target_id = int(context.args[0])
        await db.execute("UPDATE users SET heat = 0 WHERE user_id = ?", (target_id,))
        await db.commit()

        await update.message.reply_text(f"🔥 Heat Level target `{target_id}` diset kembali ke 0.", parse_mode="Markdown")

# ==========================================
# MAIN FUNCTION
# ==========================================
def build_app():
    """Membangun Application (handlers terpasang) tanpa langsung menjalankan polling.
    Dipisah dari main() supaya bot ini bisa dijalankan sendiri (standalone)
    ATAU digabung dengan bot lain dalam satu proses lewat bot_launcher.py."""
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    # Public Operations Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rekening", cmd_rekening))
    app.add_handler(CommandHandler("work", cmd_work))
    app.add_handler(CommandHandler("daily", cmd_daily))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(CommandHandler("crime", cmd_crime))
    app.add_handler(CommandHandler("bounty", cmd_bounty))
    app.add_handler(CommandHandler("hit", cmd_hit))
    app.add_handler(CommandHandler("wanted", cmd_wanted))
    app.add_handler(CommandHandler("crew", cmd_crew))

    # Admin & Cheat Commands
    app.add_handler(CommandHandler("admin_panel", cmd_admin_panel))
    app.add_handler(CommandHandler("jail_user", cmd_jail_user))
    app.add_handler(CommandHandler("unjail_user", cmd_unjail_user))
    app.add_handler(CommandHandler("clear_heat", cmd_clear_heat))
    app.add_handler(CommandHandler("cheat_godmode", cmd_cheat_godmode))
    app.add_handler(CommandHandler("cheat_instant_work", cmd_cheat_instant_work))
    app.add_handler(CommandHandler("cheat_clear_bounty", cmd_cheat_clear_bounty))

    return app

def main():
    app = build_app()
    print("⚔️ Telegram Cosa Nostra Operations Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
