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
    CallbackQueryHandler,
    MessageHandler,
    filters
)

# Set up Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("wedding_bot")

# ==========================================
# CONFIGURATION & RAILWAY ENV SETUP
# ==========================================
TOKEN = os.getenv("TELEGRAM_WEDDING_BOT_TOKEN")

if not TOKEN:
    raise ValueError("TELEGRAM_WEDDING_BOT_TOKEN belum diset di Variables Railway!")

# Channel Resmi Sesuai Permintaan
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@RoyalWeddingRP")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "wedding_event.db")
WIB = timezone(timedelta(hours=7))  # UTC+7

# ID Owner Permanen (Otomatis Tier 4 / Full Control)
MY_PERMANENT_OWNER_ID = 8396793986  

# Pricing Paket Mapping
PAKET_PRICING = {
    "silver": 500000,
    "gold": 1200000,
    "platinum": 2500000
}

# ==========================================
# HELPER KONEKSI DATABASE
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
# DATABASE SCHEMA & INITIALIZATION
# ==========================================
async def init_wedding_db():
    async with get_db_connection() as db:
        # Tabel Users, Koin & Tier Admin
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                koin INTEGER DEFAULT 1000000,
                admin_tier INTEGER DEFAULT 0,
                last_daily INTEGER DEFAULT 0
            )
        """)

        user_columns = [
            ("admin_tier", "INTEGER DEFAULT 0"),
            ("last_daily", "INTEGER DEFAULT 0")
        ]
        for col_name, col_type in user_columns:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type};")
            except Exception:
                pass
        
        # Tabel Events (Lengkap Data Diri & Paket Otomatis)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_user_id INTEGER NOT NULL,
                jenis_event TEXT,
                mempelai_pria_nama TEXT,
                mempelai_pria_umur INTEGER,
                mempelai_pria_ortu TEXT,
                mempelai_wanita_nama TEXT,
                mempelai_wanita_umur INTEGER,
                mempelai_wanita_ortu TEXT,
                tgl_jam TEXT,
                lokasi TEXT,
                est_tamu INTEGER,
                paket TEXT DEFAULT 'gold',
                tema TEXT DEFAULT 'Royal Classic',
                vendor_catering TEXT DEFAULT 'Standar',
                vendor_mua TEXT DEFAULT 'Standar',
                vendor_dekor TEXT DEFAULT 'Standar',
                status TEXT DEFAULT 'Pending',
                total_biaya INTEGER DEFAULT 1200000,
                created_at INTEGER
            )
        """)

        # Tabel RSVP Tamu
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rsvp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                status_rsvp TEXT,
                kategori TEXT DEFAULT 'Umum',
                meja TEXT DEFAULT 'Belum Diatur'
            )
        """)

        # Tabel Angpao Digital
        await db.execute("""
            CREATE TABLE IF NOT EXISTS angpao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                from_user_id INTEGER NOT NULL,
                from_username TEXT,
                jumlah_koin INTEGER,
                pesan TEXT,
                created_at INTEGER
            )
        """)

        # Tabel Klaim Hadiah / Doorprize / Angpao ke Admin
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                tipe_klaim TEXT,
                nominal INTEGER,
                status TEXT DEFAULT 'Pending',
                created_at INTEGER
            )
        """)

        # Tabel Request Lagu
        await db.execute("""
            CREATE TABLE IF NOT EXISTS song_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                judul_lagu TEXT,
                status TEXT DEFAULT 'Pending'
            )
        """)

        # Tabel Galeri Photo Booth
        await db.execute("""
            CREATE TABLE IF NOT EXISTS photo_gallery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                photo_url TEXT,
                caption TEXT
            )
        """)

        await db.commit()

async def post_init(application):
    await init_wedding_db()

# ==========================================
# HELPER FUNCTIONS
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

async def send_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        if CHANNEL_ID:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Gagal mengirim pesan ke channel {CHANNEL_ID}: {e}")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>TERJADI KENDALA OPERASIONAL!</b>\n\n"
            "Sistem event organizer mengalami masalah koneksi sementara. Silakan coba kembali perintah Anda.",
            parse_mode="HTML"
        )

# ==========================================
# KEYBOARD BUILDERS
# ==========================================
def get_main_menu():
    keyboard = [
        [
            InlineKeyboardButton("📋 Paket & Harga", callback_data="menu_paket"),
            InlineKeyboardButton("🎉 Buat Event", callback_data="menu_buat_event")
        ],
        [
            InlineKeyboardButton("🧑‍🤝‍🧑 Vendor & Role", callback_data="menu_vendor"),
            InlineKeyboardButton("💌 Tamu & RSVP", callback_data="menu_rsvp")
        ],
        [
            InlineKeyboardButton("⏱️ Rundown", callback_data="menu_rundown"),
            InlineKeyboardButton("💰 Koin & Transaksi", callback_data="menu_koin")
        ],
        [
            InlineKeyboardButton("🎁 Angpao & Doorprize", callback_data="menu_angpao"),
            InlineKeyboardButton("🎵 Music Request", callback_data="menu_musik")
        ],
        [
            InlineKeyboardButton("📸 Photobooth", callback_data="menu_photobooth"),
            InlineKeyboardButton("🍽️ Layanan Meja Grup", callback_data="menu_layanan")
        ],
        [
            InlineKeyboardButton("📊 Laporan Acara", callback_data="menu_laporan"),
            InlineKeyboardButton("⚙️ Admin Panel", callback_data="menu_admin")
        ],
        [
            InlineKeyboardButton("❓ Bantuan", callback_data="menu_bantuan")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def btn_back():
    return InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")

# ==========================================
# START & CALLBACK ROUTER
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO users (user_id, username) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username)
        )
        await db.commit()

    text = (
        "✨ <b>ROYAL WEDDING ORGANIZER & EVENT SIMULATOR (RP)</b> ✨\n"
        "──────────────────────────────────────────\n"
        "<i>\"Mewujudkan Impian Pernikahan Pasangan Pengantin Tanpa Batas\"</i>\n\n"
        "Selamat datang di Sistem Manajemen Event & Roleplay! "
        "Fasilitas ini terintegrasi langsung dengan channel <b>@RoyalWeddingRP</b>. "
        "Kelola paket resepsi, klaim doorprize, angpao, hingga layanan makanan di grup melalui menu di bawah ini:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=get_main_menu(), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=get_main_menu(), parse_mode="HTML")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    username = query.from_user.username or query.from_user.first_name

    if data == "menu_main":
        await start(update, context)

    # --- 1. PAKET & HARGA ---
    elif data == "menu_paket":
        text = "📋 <b>KATALOG PAKET & PRICING EVENT</b>\n\nPilih jenis paket untuk melihat rincian fasilitas & biaya otomatis:"
        keyboard = [
            [
                InlineKeyboardButton("Silver (500k)", callback_data="pkt_silver"),
                InlineKeyboardButton("Gold (1.2jt)", callback_data="pkt_gold"),
                InlineKeyboardButton("Platinum (2.5jt)", callback_data="pkt_platinum")
            ],
            [
                InlineKeyboardButton("📊 Perbandingan Paket", callback_data="pkt_banding")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_silver":
        text = (
            "🤍 <b>PAKET SILVER — \"Simple & Sakral\"</b>\n"
            "💵 Biaya Otomatis: <b>500.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 100 Tamu\n"
            "• Catering prasmanan 3 menu utama\n"
            "• Dekorasi minimalis 1 warna tema\n"
            "• MC + Sound System Standar\n"
            "• Dokumentasi Foto Only"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_gold":
        text = (
            "💛 <b>PAKET GOLD — \"Elegant Wedding\"</b>\n"
            "💵 Biaya Otomatis: <b>1.200.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 250 Tamu\n"
            "• Catering Prasmanan 5 menu utama + 2 snack corner\n"
            "• Dekorasi tema custom (Pilihan dari 5 tema)\n"
            "• MC + Band Akustik\n"
            "• Dokumentasi Foto + Video (SDE)\n"
            "• MUA + Busana Pengantin Pria & Wanita"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_platinum":
        text = (
            "💎 <b>PAKET PLATINUM — \"Royal Celebration\"</b>\n"
            "💵 Biaya Otomatis: <b>2.500.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 500+ Tamu (Indoor + Outdoor)\n"
            "• Full course dinner 8 menu + 4 corner\n"
            "• Dekorasi Premium + Lighting Artistik\n"
            "• MC Bilingual + Live Band / Orkestra\n"
            "• Dokumentasi Lengkap (Drone, SDE, Dokumenter)\n"
            "• MUA + Busana Pria & Wanita 3x ganti"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_banding":
        text = (
            "📊 <b>PERBANDINGAN FASILITAS & BIAYA</b>\n\n"
            "<pre>"
            "+---------------+----------+----------+------------------+\n"
            "| Fitur         | Silver   | Gold     | Platinum         |\n"
            "+---------------+----------+----------+------------------+\n"
            "| Biaya Koin    | 500.000  | 1.200.000| 2.500.000        |\n"
            "| Kapasitas     | 100 Tamu | 250 Tamu | 500+ Tamu        |\n"
            "| Menu Utama    | 3        | 5        | 8                |\n"
            "| Dokumentasi   | Foto     | Foto+SDE | Full+Drone       |\n"
            "| MUA           | ❌       | 1x       | 3x               |\n"
            "+---------------+----------+----------+------------------"
            "</pre>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 2. BUAT EVENT ---
    elif data == "menu_buat_event":
        text = "🎉 <b>PERENCANAAN & PEMBUATAN EVENT</b>\n\nPilih langkah operasional pembuatan acara:"
        keyboard = [
            [
                InlineKeyboardButton("➕ Buat Acara Baru", callback_data="evt_baru"),
                InlineKeyboardButton("📅 Cek Tanggal Kosong", callback_data="evt_tanggal")
            ],
            [
                InlineKeyboardButton("🌸 Pilih Tema Dekor", callback_data="evt_tema"),
                InlineKeyboardButton("✅ Konfirmasi Booking", callback_data="evt_konfirmasi")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_baru":
        text = (
            "📝 <b>FORMULIR PEMBUATAN EVENT & PAKET</b>\n\n"
            "Gunakan sintaks komando lengkap berikut:\n"
            "<code>/buat_event [Paket: silver/gold/platinum] | [Jenis] | [Nama Pria, Umur, Ortu] | [Nama Wanita, Umur, Ortu] | [Tgl&Jam] | [Lokasi] | [Est.Tamu]</code>\n\n"
            "<i>Contoh:</i>\n"
            "<code>/buat_event gold | Pernikahan | Sora Pratama, 26, Bp. Hendra | Hana Amelia, 24, Bp. Wijaya | 20-12-2026 10:00 | Grand Ballroom | 300</code>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_tanggal":
        text = (
            "📅 <b>JADWAL EVENT TERDAFTAR</b>\n\n"
            "• 20-12-2026 : 🔴 Terisi (Pernikahan Sora & Hana)\n"
            "• 25-12-2026 : 🟢 Kosong (Tersedia)\n"
            "• 05-01-2027 : 🟢 Kosong (Tersedia)"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_tema":
        text = (
            "🌺 <b>KATALOG TEMA DEKORASI EVENT</b>\n\n"
            "🌸 <b>Rustic Garden:</b> Dominan hijau-putih, elemen kayu, bunga liar.\n"
            "🕊️ <b>Modern Minimalist:</b> Monokrom, garis bersih, geometri.\n"
            "🏛️ <b>Royal Classic:</b> Marun & gold, kursi tiffany, lampu kristal.\n"
            "🌿 <b>Tropical Bali:</b> Bunga kamboja, kain tenun, nuansa pantai."
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_konfirmasi":
        async with get_db_connection() as db:
            async with db.execute(
                """SELECT event_id, jenis_event, paket, mempelai_pria_nama, mempelai_wanita_nama, tgl_jam, status, total_biaya 
                   FROM events WHERE host_user_id = ? ORDER BY event_id DESC LIMIT 1""", 
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()

        if row:
            e_id, jenis, pkt, p_nama, w_nama, tgl, status, biaya = row
            text = (
                f"✅ <b>RINGKASAN EVENT #{e_id}</b>\n"
                "──────────────────────────\n"
                f"• <b>Status Event     :</b> {status}\n"
                f"• <b>Paket Dipilih    :</b> Paket {pkt.upper()}\n"
                f"• <b>Mempelai         :</b> {p_nama} & {w_nama}\n"
                f"• <b>Tanggal & Jam    :</b> {tgl}\n"
                f"• <b>Total Biaya      :</b> {biaya:,} Koin\n"
                "──────────────────────────\n"
                "<i>Ketik /bayar_event untuk melunasi biaya booking!</i>"
            )
        else:
            text = "❌ Belum ada draf event aktif. Buat acara baru terlebih dahulu."

        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 3. VENDOR & ROLE ---
    elif data == "menu_vendor":
        text = "🧑‍🤝‍🧑 <b>DIREKTORI VENDOR & OPERASIONAL ROLE</b>\n\nPilih kategori vendor untuk interaksi RP:"
        keyboard = [
            [InlineKeyboardButton("🎤 MC", callback_data="ven_mc"), InlineKeyboardButton("🍽️ Catering", callback_data="ven_catering")],
            [InlineKeyboardButton("🥤 Minuman", callback_data="ven_minuman"), InlineKeyboardButton("🎋 Dekorasi", callback_data="ven_dekor")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ven_mc":
        text = (
            "🎤 <b>ROLE MASTER OF CEREMONY (MC)</b>\n\n"
            "• <code>/mc_buka</code> — Pembukaan acara\n"
            "• <code>/mc_ijab</code> — Memandu Ijab Qabul\n"
            "• <code>/mc_resepsi</code> — Sambutan resepsi\n"
            "• <code>/mc_sungkem</code> — Momen sungkeman\n"
            "• <code>/mc_tutup</code> — Penutupan"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_vendor")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ven_catering":
        text = "🍽️ <b>CATERING DINET:</b> Gunakan <code>/minta_makan [menu]</code> di grup untuk memesan hidangan."
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_vendor")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 4. ANGPAO & KLAIM ---
    elif data == "menu_angpao":
        text = (
            "🎁 <b>ANGPAO & KLAIM HADIAH DOORPRIZE</b>\n\n"
            "Kirim angpao atau klaim hadiah ke Admin melalui komando berikut:\n\n"
            "• <code>/angpao [jumlah] | [pesan_doa]</code> — Kirim Angpao\n"
            "• <code>/klaim_doorprize [nominal_hadiah]</code> — Klaim Hadiah Doorprize\n"
            "• <code>/klaim_angpao [nominal]</code> — Tarik Saldo Angpao Masuk\n"
            "• <code>/bukutamu</code> — Lihat Buku Tamu & Doa"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 5. MUSIC REQUEST ---
    elif data == "menu_musik":
        text = (
            "🎵 <b>REQUEST LAGU KE BAND PESTA</b>\n\n"
            "• <code>/request_lagu [Judul Lagu - Penyanyi]</code>\n"
            "• <code>/daftar_lagu</code> — Cek Queue Antrean"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 6. PHOTOBOOTH ---
    elif data == "menu_photobooth":
        text = (
            "📸 <b>DIGITAL PHOTO BOOTH & GALLERY</b>\n\n"
            "• <code>/photobooth [caption]</code> — Upload foto pose & kirim otomatis ke @RoyalWeddingRP\n"
            "• <code>/gallery</code> — Lihat galeri tamu"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 7. LAYANAN GRUP ---
    elif data == "menu_layanan":
        text = (
            "🍽️ <b>LAYANAN MAKANAN & MINUMAN DI GRUP</b>\n\n"
            "• <code>/minta_makan [menu]</code>\n"
            "• <code>/minta_minum [menu]</code>\n\n"
            "💌 <b>Konfirmasi RSVP Cepat:</b>"
        )
        rsvp_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Hadir", callback_data="rsvp_hadir_btn"),
                InlineKeyboardButton("❌ Tidak Hadir", callback_data="rsvp_tidak_btn")
            ],
            [btn_back()]
        ])
        await query.edit_message_text(text, reply_markup=rsvp_markup, parse_mode="HTML")

    # --- 8. TAMU & RSVP ---
    elif data == "menu_rsvp":
        async with get_db_connection() as db:
            async with db.execute("SELECT username, status_rsvp, meja FROM rsvp WHERE event_id = 1") as cursor:
                rows = await cursor.fetchall()

        list_tamu = ""
        if rows:
            for uname, st, meja in rows:
                list_tamu += f"• @{uname} — <b>{st.upper()}</b> (Meja: {meja})\n"
        else:
            list_tamu = "<i>Belum ada konfirmasi RSVP.</i>"

        text = (
            "💌 <b>MANAJEMEN TAMU & RSVP</b>\n\n"
            f"<b>Daftar Kehadiran:</b>\n{list_tamu}"
        )
        rsvp_inline = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Konfirmasi Hadir", callback_data="rsvp_hadir_btn"),
                InlineKeyboardButton("❌ Konfirmasi Tidak Hadir", callback_data="rsvp_tidak_btn")
            ],
            [btn_back()]
        ])
        await query.edit_message_text(text, reply_markup=rsvp_inline, parse_mode="HTML")

    elif data in ("rsvp_hadir_btn", "rsvp_tidak_btn"):
        st_val = "hadir" if data == "rsvp_hadir_btn" else "tidak"
        async with get_db_connection() as db:
            await db.execute("INSERT INTO rsvp (event_id, user_id, username, status_rsvp) VALUES (1, ?, ?, ?)", (user_id, username, st_val))
            await db.commit()

        await query.edit_message_text(
            f"💌 Terima kasih @{username}! Status RSVP Anda: **{st_val.upper()}**.",
            reply_markup=InlineKeyboardMarkup([[btn_back()]]),
            parse_mode="HTML"
        )

    # --- 9. RUNDOWN ---
    elif data == "menu_rundown":
        text = "⏱️ <b>RUNDOWN:</b> Persiapan ➔ Akad ➔ <b>[Resepsi]</b> ➔ Hiburan ➔ Selesai"
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 10. KOIN & TRANSAKSI ---
    elif data == "menu_koin":
        async with get_db_connection() as db:
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                koin = row[0] if row else 0

        text = (
            "💰 <b>KOIN & TRANSAKSI</b>\n\n"
            f"• <b>Saldo Anda :</b> <b>{koin:,} Koin</b>\n"
            "• <code>/transfer_koin [user_id] [jumlah]</code>\n"
            "• <code>/daily_koin</code> — Klaim harian (+50k)"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 11. LAPORAN ---
    elif data == "menu_laporan":
        text = "📊 <b>LAPORAN EVENT:</b> Berjalan sukses dengan rating ⭐⭐⭐⭐⭐ (5.0/5.0)."
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 12. ADMIN PANEL ---
    elif data == "menu_admin":
        async with get_db_connection() as db:
            tier = await check_admin_tier(db, user_id)

        if tier < 1:
            return await query.edit_message_text("🚫 <b>AKSES DITOLAK:</b> Khusus Admin.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

        text = (
            f"⚙️ <b>ADMIN CONTROL PANEL (Tier {tier})</b>\n\n"
            "• <code>/set_admin [user_id] [tier]</code>\n"
            "• <code>/list_klaim</code> — Lihat daftar klaim hadiah/angpao\n"
            "• <code>/verifikasi_klaim [claim_id] [approve/reject]</code>\n"
            "• <code>/doorprize</code> — Undi doorprize acak\n"
            "• <code>/reset_event</code> — Reset data event"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 13. BANTUAN ---
    elif data == "menu_bantuan":
        text = "❓ <b>BANTUAN:</b> Gunakan tombol interaktif atau command untuk mengontrol simulasi pernikahan di @RoyalWeddingRP."
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==========================================
# FITUR KLAIM HADIAH, ANGPAO & ADMIN VERIFIKASI
# ==========================================
async def cmd_klaim_doorprize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/klaim_doorprize [nominal]</code>", parse_mode="HTML")
    
    nominal = int(context.args[0])
    now = int(time.time())

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO claims (user_id, username, tipe_klaim, nominal, status, created_at) VALUES (?, ?, 'Doorprize', ?, 'Pending', ?)",
            (user_id, username, nominal, now)
        )
        await db.commit()

    await update.message.reply_text(f"📝 Permintaan klaim Doorprize senilai <b>{nominal:,} Koin</b> telah dikirim ke Admin untuk diverifikasi.", parse_mode="HTML")
    await send_to_channel(context, f"🔔 <b>KLAIM DOORPRIZE BARU</b>\n👤 @{username} mengajukan klaim doorprize senilai <b>{nominal:,} Koin</b>.")

async def cmd_klaim_angpao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Format: <code>/klaim_angpao [nominal]</code>", parse_mode="HTML")
    
    nominal = int(context.args[0])
    now = int(time.time())

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO claims (user_id, username, tipe_klaim, nominal, status, created_at) VALUES (?, ?, 'Angpao', ?, 'Pending', ?)",
            (user_id, username, nominal, now)
        )
        await db.commit()

    await update.message.reply_text(f"📝 Permintaan pencairan Angpao senilai <b>{nominal:,} Koin</b> telah dikirim ke Admin.", parse_mode="HTML")

async def cmd_list_klaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Khusus Admin.")

        async with db.execute("SELECT claim_id, username, tipe_klaim, nominal FROM claims WHERE status = 'Pending'") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return await update.message.reply_text("📋 Tidak ada antrean klaim pending.")

    lines = ["📋 <b>DAFTAR KLAIM PENDING</b>\n"]
    for cid, uname, tipe, nom in rows:
        lines.append(f"• ID: <code>{cid}</code> | @{uname} | {tipe} | <b>{nom:,} Koin</b>\n  ↳ Gunakan: <code>/verifikasi_klaim {cid} approve</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_verifikasi_klaim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        tier = await check_admin_tier(db, user_id)
        if tier < 1:
            return await update.message.reply_text("🚫 Khusus Admin.")

        if len(context.args) < 2 or not context.args[0].isdigit():
            return await update.message.reply_text("Format: <code>/verifikasi_klaim [claim_id] [approve/reject]</code>", parse_mode="HTML")

        cid = int(context.args[0])
        action = context.args[1].lower()

        async with db.execute("SELECT user_id, tipe_klaim, nominal, status FROM claims WHERE claim_id = ?", (cid,)) as cursor:
            claim = await cursor.fetchone()

        if not claim or claim[3] != 'Pending':
            return await update.message.reply_text("❌ Klaim tidak ditemukan atau sudah diproses.")

        t_id, tipe, nom, _ = claim

        if action == "approve":
            await db.execute("UPDATE claims SET status = 'Approved' WHERE claim_id = ?", (cid,))
            await db.execute("INSERT INTO users (user_id, koin) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET koin=koin+?", (t_id, nom, nom))
            await db.commit()
            await update.message.reply_text(f"✅ Klaim #{cid} disetujui. Saldo <b>{nom:,} Koin</b> telah ditambahkan ke user.")
        else:
            await db.execute("UPDATE claims SET status = 'Rejected' WHERE claim_id = ?", (cid,))
            await db.commit()
            await update.message.reply_text(f"❌ Klaim #{cid} ditolak.")

# ==========================================
# COMMANDS LAINNYA
# ==========================================
async def cmd_buat_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw_text = " ".join(context.args)

    if "|" not in raw_text:
        return await update.message.reply_text(
            "❌ <b>Format Salah!</b>\n\n"
            "Gunakan format:\n"
            "<code>/buat_event [paket] | [Jenis] | [Pria, Umur, Ortu] | [Wanita, Umur, Ortu] | [Tgl] | [Lokasi] | [Tamu]</code>",
            parse_mode="HTML"
        )

    parts = [p.strip() for p in raw_text.split("|")]
    if len(parts) < 7:
        return await update.message.reply_text("❌ Parameter kurang lengkap. Harus ada 7 bagian dipisahkan <code>|</code>.", parse_mode="HTML")

    paket = parts[0].lower()
    if paket not in PAKET_PRICING:
        paket = "gold"

    biaya_otomatis = PAKET_PRICING[paket]
    jenis, pria_raw, wanita_raw, tgl, lokasi, est_tamu = parts[1:7]
    
    pria_parts = [x.strip() for x in pria_raw.split(",")]
    p_nama = pria_parts[0] if len(pria_parts) > 0 else "Pria"
    p_umur = int(pria_parts[1]) if len(pria_parts) > 1 and pria_parts[1].isdigit() else 25
    p_ortu = pria_parts[2] if len(pria_parts) > 2 else "Keluarga"

    wanita_parts = [x.strip() for x in wanita_raw.split(",")]
    w_nama = wanita_parts[0] if len(wanita_parts) > 0 else "Wanita"
    w_umur = int(wanita_parts[1]) if len(wanita_parts) > 1 and wanita_parts[1].isdigit() else 23
    w_ortu = wanita_parts[2] if len(wanita_parts) > 2 else "Keluarga"

    est_tamu_num = int(est_tamu) if est_tamu.isdigit() else 100
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO events (host_user_id, jenis_event, paket, mempelai_pria_nama, mempelai_pria_umur, mempelai_pria_ortu,
                                 mempelai_wanita_nama, mempelai_wanita_umur, mempelai_wanita_ortu, tgl_jam, lokasi, est_tamu, status, total_biaya, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)""",
            (user_id, jenis, paket, p_nama, p_umur, p_ortu, w_nama, w_umur, w_ortu, tgl, lokasi, est_tamu_num, biaya_otomatis, now_epoch)
        )
        await db.commit()

    channel_msg = (
        f"📢 <b>ANNOUNCEMENT: NEW WEDDING EVENT REGISTERED!</b>\n"
        "──────────────────────────\n"
        f"📦 <b>Paket Pesta    :</b> Paket {paket.upper()} ({biaya_otomatis:,} Koin)\n"
        f"🤵 <b>Mempelai Pria  :</b> {p_nama} ({p_umur} Thn)\n"
        f"👰 <b>Mempelai Wanita:</b> {w_nama} ({w_umur} Thn)\n"
        "──────────────────────────\n"
        f"📍 <b>Lokasi Pesta   :</b> {lokasi}\n"
        f"📅 <b>Tanggal & Jam :</b> {tgl}\n"
        "──────────────────────────\n"
        "<i>Konfirmasi kehadiran Anda melalui komando /rsvp di @RoyalWeddingRP!</i>"
    )
    await send_to_channel(context, channel_msg)
    await update.message.reply_text(f"🎉 Event berhasil dicatat dengan Paket **{paket.upper()}** (Total Biaya: **{biaya_otomatis:,} Koin**) dan dikirim ke {CHANNEL_ID}!", parse_mode="HTML")

async def cmd_bayar_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        async with db.execute("SELECT event_id, total_biaya FROM events WHERE host_user_id = ? AND status = 'Pending' ORDER BY event_id DESC LIMIT 1", (user_id,)) as cursor:
            event = await cursor.fetchone()

        if not event:
            return await update.message.reply_text("❌ Tidak ada tagihan event aktif.")

        event_id, total_biaya = event
        async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            user_row = await cursor.fetchone()
            user_koin = user_row[0] if user_row else 0

        if user_koin < total_biaya:
            return await update.message.reply_text(f"❌ Saldo tidak cukup! Butuh {total_biaya:,} Koin.")

        await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (total_biaya, user_id))
        await db.execute("UPDATE events SET status = 'Paid' WHERE event_id = ?", (event_id,))
        await db.commit()

    await update.message.reply_text(f"✅ Event #{event_id} lunas! Status menjadi **PAID**.", parse_mode="HTML")

async def cmd_minta_makan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    menu = " ".join(context.args).strip() or "Prasmanan Spesial"
    await update.message.reply_text(f"🍽️ Pramusaji mengantar pesanan makanan <b>\"{menu}\"</b> untuk @{username}!", parse_mode="HTML")

async def cmd_minta_minum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    minum = " ".join(context.args).strip() or "Mocktail Segar"
    await update.message.reply_text(f"🥤 Bartender menyajikan minuman <b>\"{minum}\"</b> untuk @{username}. Cheers! 🥂", parse_mode="HTML")

async def cmd_photobooth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    caption = " ".join(context.args).strip() or "Pose Anggun di Resepsi"

    async with get_db_connection() as db:
        await db.execute("INSERT INTO photo_gallery (event_id, user_id, username, photo_url, caption) VALUES (1, ?, ?, 'placeholder', ?)", (update.effective_user.id, username, caption))
        await db.commit()

    await send_to_channel(context, f"📸 <b>PHOTOBOOTH FEED (@RoyalWeddingRP)</b>\n👤 @{username}\n💬 <i>\"{caption}\"</i>")
    await update.message.reply_text(f"📸 Foto @{username} berhasil masuk galeri dan diposting ke {CHANNEL_ID}!", parse_mode="HTML")

async def cmd_angpao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    raw = " ".join(context.args)

    if "|" not in raw:
        return await update.message.reply_text("Format: <code>/angpao [jumlah] | [pesan]</code>", parse_mode="HTML")

    parts = [p.strip() for p in raw.split("|", 1)]
    jumlah = int(parts[0]) if parts[0].isdigit() else 10000
    pesan = parts[1]
    now = int(time.time())

    async with get_db_connection() as db:
        async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            bal = row[0] if row else 0
        if bal < jumlah:
            return await update.message.reply_text("❌ Saldo koin tidak cukup.")
        await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (jumlah, user_id))
        await db.execute("INSERT INTO angpao (event_id, from_user_id, from_username, jumlah_koin, pesan, created_at) VALUES (1, ?, ?, ?, ?, ?)", (user_id, username, jumlah, pesan, now))
        await db.commit()

    await send_to_channel(context, f"🎁 <b>ANGPAO & WISHING (@RoyalWeddingRP)</b>\n✉️ Dari: @{username}\n💰 <b>{jumlah:,} Koin</b>\n🕊️ <i>\"{pesan}\"</i>")
    await update.message.reply_text(f"🎁 Angpao <b>{jumlah:,} Koin</b> berhasil dikirim!", parse_mode="HTML")

async def cmd_bukutamu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT from_username, jumlah_koin, pesan FROM angpao WHERE event_id = 1 ORDER BY id DESC LIMIT 10") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return await update.message.reply_text("📖 Buku tamu kosong.")
    lines = ["📖 <b>BUKU TAMU & HARAPAN</b>\n"]
    for u, k, m in rows:
        lines.append(f"• @{u} (🎁 {k:,} Koin): <i>\"{m}\"</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_request_lagu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username or update.effective_user.first_name
    judul = " ".join(context.args).strip()
    if not judul:
        return await update.message.reply_text("Format: <code>/request_lagu [Judul Lagu - Penyanyi]</code>", parse_mode="HTML")
    async with get_db_connection() as db:
        await db.execute("INSERT INTO song_requests (event_id, user_id, username, judul_lagu) VALUES (1, ?, ?, ?)", (update.effective_user.id, username, judul))
        await db.commit()
    await update.message.reply_text(f"🎵 Lagu **\"{judul}\"** masuk antrean band!", parse_mode="HTML")

async def cmd_daftar_lagu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT username, judul_lagu, status FROM song_requests WHERE event_id = 1") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return await update.message.reply_text("🎵 Antrean lagu kosong.")
    lines = ["🎵 <b>ANTRAN LAGU BAND</b>\n"]
    for u, l, s in rows:
        lines.append(f"• {l} (req @{u}) — [{s}]")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_gallery(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT username, caption FROM photo_gallery WHERE event_id = 1 ORDER BY id DESC LIMIT 10") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return await update.message.reply_text("🖼️ Galeri kosong.")
    lines = ["🖼️ <b>GALERI FOTO</b>\n"]
    for u, c in rows:
        lines.append(f"• @{u}: <i>\"{c}\"</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_daily_koin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = int(time.time())
    async with get_db_connection() as db:
        async with db.execute("SELECT last_daily FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            last = row[0] if row and row[0] else 0
        if now - last < 86400:
            return await update.message.reply_text("⏳ Tunjangan harian sudah diklaim hari ini.")
        await db.execute("UPDATE users SET koin = koin + 50000, last_daily = ? WHERE user_id = ?", (now, user_id))
        await db.commit()
    await update.message.reply_text("💵 Klaim koin harian +50,000 berhasil!", parse_mode="HTML")

async def cmd_transfer_koin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        return await update.message.reply_text("Format: <code>/transfer_koin [id] [jumlah]</code>", parse_mode="HTML")
    target, amount = int(context.args[0]), int(context.args[1])
    user_id = update.effective_user.id
    async with get_db_connection() as db:
        async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            bal = row[0] if row else 0
        if bal < amount:
            return await update.message.reply_text("❌ Saldo tidak cukup.")
        await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (amount, user_id))
        await db.execute("INSERT INTO users (user_id, koin) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET koin=koin+?", (target, amount, amount))
        await db.commit()
    await update.message.reply_text(f"💸 Berhasil transfer {amount:,} koin ke `{target}`.", parse_mode="HTML")

async def cmd_doorprize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT username FROM rsvp WHERE event_id = 1 AND status_rsvp = 'hadir'") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return await update.message.reply_text("🎲 Belum ada tamu hadir.")
    winner = random.choice(rows)[0]
    await send_to_channel(context, f"🎲 <b>LUCKY DRAW WINNER (@RoyalWeddingRP)</b>\n🏆 Pemenang Doorprize: @{winner}! Silakan gunakan /klaim_doorprize untuk mencairkan hadiah.")
    await update.message.reply_text(f"🎲 **Pemenang Doorprize:** @{winner}!", parse_mode="HTML")

async def cmd_mc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.split()[0].replace("/", "")
    resp = {
        "mc_buka": "🎤 *\"Selamat datang di resepsi pernikahan...\"*",
        "mc_ijab": "🎤 *\"Prosesi akad nikah dimulai...\"*",
        "mc_resepsi": "🎤 *\"Selamat datang kedua mempelai di pelaminan!\"*",
        "mc_sungkem": "🎤 *\"Sesi sungkeman penuh haru...\"*",
        "mc_tutup": "🎤 *\"Acara selesai, terima kasih!\"*"
    }
    await update.message.reply_text(resp.get(cmd, "🎤 MC bersiap."), parse_mode="HTML")

async def cmd_incident(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Format: <code>/incident [hujan|catering_telat|sound_dead]</code>", parse_mode="HTML")
    inc = {"hujan": "☔ Hujan turun!", "catering_telat": "🍽️ Catering terlambat!", "sound_dead": "🎤 Sound mati!"}
    await update.message.reply_text(inc.get(context.args[0].lower(), "⚠️ Insiden terjadi!"), parse_mode="HTML")

async def cmd_set_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        if await check_admin_tier(db, update.effective_user.id) < 4:
            return await update.message.reply_text("🚫 Khusus Owner Utama (Tier 4).")
        if len(context.args) < 2:
            return await update.message.reply_text("Format: <code>/set_admin [user_id] [tier]</code>", parse_mode="HTML")
        await db.execute("INSERT INTO users (user_id, admin_tier) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET admin_tier=?", (int(context.args[0]), int(context.args[1]), int(context.args[1])))
        await db.commit()
    await update.message.reply_text("✅ Status admin diperbarui.")

async def cmd_reset_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        if await check_admin_tier(db, update.effective_user.id) < 2:
            return await update.message.reply_text("🚫 Khusus Admin.")
        await db.execute("DELETE FROM events")
        await db.execute("DELETE FROM rsvp")
        await db.execute("DELETE FROM angpao")
        await db.execute("DELETE FROM claims")
        await db.commit()
    await update.message.reply_text("🔄 Data event berhasil direset total.", parse_mode="HTML")

# ==========================================
# BUILD APPLICATION & POLLING
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))

    app.add_handler(CommandHandler("buat_event", cmd_buat_event))
    app.add_handler(CommandHandler("bayar_event", cmd_bayar_event))
    app.add_handler(CommandHandler("pilih_vendor", lambda u, c: u.message.reply_text("Gunakan /buat_event dengan paket yang sesuai.")))
    app.add_handler(CommandHandler("atur_meja", lambda u, c: u.message.reply_text("Fitur diatur melalui menu utama.")))
    app.add_handler(CommandHandler("rsvp", cmd_rsvp))

    app.add_handler(CommandHandler("minta_makan", cmd_minta_makan))
    app.add_handler(CommandHandler("minta_minum", cmd_minta_minum))

    app.add_handler(CommandHandler("angpao", cmd_angpao))
    app.add_handler(CommandHandler("bukutamu", cmd_bukutamu))
    app.add_handler(CommandHandler("request_lagu", cmd_request_lagu))
    app.add_handler(CommandHandler("daftar_lagu", cmd_daftar_lagu))
    app.add_handler(CommandHandler("photobooth", cmd_photobooth))
    app.add_handler(CommandHandler("gallery", cmd_gallery))
    app.add_handler(CommandHandler("daily_koin", cmd_daily_koin))
    app.add_handler(CommandHandler("transfer_koin", cmd_transfer_koin))

    app.add_handler(CommandHandler("klaim_doorprize", cmd_klaim_doorprize))
    app.add_handler(CommandHandler("klaim_angpao", cmd_klaim_angpao))
    app.add_handler(CommandHandler("list_klaim", cmd_list_klaim))
    app.add_handler(CommandHandler("verifikasi_klaim", cmd_verifikasi_klaim))

    app.add_handler(CommandHandler(["mc_buka", "mc_ijab", "mc_resepsi", "mc_sungkem", "mc_tutup"], cmd_mc))

    app.add_handler(CommandHandler("doorprize", cmd_doorprize))
    app.add_handler(CommandHandler("set_admin", cmd_set_admin))
    app.add_handler(CommandHandler("incident", cmd_incident))
    app.add_handler(CommandHandler("reset_event", cmd_reset_event))

    return app

def main():
    asyncio.run(init_wedding_db())
    app = build_app()
    print("🤖 Bot Wedding Organizer & Event RP (@RoyalWeddingRP Linked) Running...")
    app.run_polling()

if __name__ == "__main__":
    main()
