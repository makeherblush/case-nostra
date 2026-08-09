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

CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "@RoyalWeddingRP")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "wedding_event.db")
WIB = timezone(timedelta(hours=7))  # UTC+7

MY_PERMANENT_OWNER_ID = 8396793986  

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
        # Tabel Pengguna
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                koin INTEGER DEFAULT 1000000,
                admin_tier INTEGER DEFAULT 0,
                last_daily INTEGER DEFAULT 0,
                created_at INTEGER
            )
        """)

        # Tabel Log Transaksi Koin
        await db.execute("""
            CREATE TABLE IF NOT EXISTS koin_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                tipe_transaksi TEXT NOT NULL,
                keterangan TEXT,
                created_at INTEGER NOT NULL
            )
        """)
        
        # Tabel Events
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
                channel_msg_id INTEGER,
                created_at INTEGER
            )
        """)

        # Tabel RSVP
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rsvp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                status_rsvp TEXT,
                kategori TEXT DEFAULT 'Umum',
                meja TEXT DEFAULT 'Belum Diatur',
                created_at INTEGER
            )
        """)

        # Tabel Angpao / Donasi
        await db.execute("""
            CREATE TABLE IF NOT EXISTS angpao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                from_user_id INTEGER NOT NULL,
                from_username TEXT,
                tipe TEXT DEFAULT 'Angpao',
                jumlah_koin INTEGER,
                pesan TEXT,
                channel_msg_id INTEGER,
                created_at INTEGER
            )
        """)

        # Tabel Klaim
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                tipe_klaim TEXT,
                nominal INTEGER,
                status TEXT DEFAULT 'Pending',
                admin_id INTEGER,
                channel_msg_id INTEGER,
                created_at INTEGER,
                processed_at INTEGER
            )
        """)

        # Tabel Song Requests
        await db.execute("""
            CREATE TABLE IF NOT EXISTS song_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                judul_lagu TEXT,
                status TEXT DEFAULT 'Pending',
                created_at INTEGER
            )
        """)

        # Tabel Photo Gallery (Photobooth)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS photo_gallery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER DEFAULT 1,
                user_id INTEGER NOT NULL,
                username TEXT,
                file_id TEXT NOT NULL,
                caption TEXT,
                channel_msg_id INTEGER,
                created_at INTEGER
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

async def log_koin_transaction(db, user_id: int, amount: int, tipe: str, keterangan: str):
    now = int(time.time())
    await db.execute(
        "INSERT INTO koin_transactions (user_id, amount, tipe_transaksi, keterangan, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, amount, tipe, keterangan, now)
    )

async def send_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str = None, photo_file_id: str = None, reply_markup=None):
    """Mengirim pesan atau gambar ke Channel Telegram utama."""
    if not CHANNEL_ID:
        return None
    try:
        if photo_file_id:
            msg = await context.bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=photo_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        else:
            msg = await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup
            )
        return msg.message_id
    except Exception as e:
        logger.error(f"Gagal mengirim media/pesan ke channel {CHANNEL_ID}: {e}")
        return None

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>TERJADI KENDALA OPERASIONAL!</b>\n\n"
            "Sistem event organizer mengalami gangguan koneksi sementara. Silakan coba kembali.",
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
            InlineKeyboardButton("🧑‍🤝‍🧑 Vendor & MC", callback_data="menu_vendor"),
            InlineKeyboardButton("💌 Tamu & RSVP", callback_data="menu_rsvp")
        ],
        [
            InlineKeyboardButton("⏱️ Rundown Acara", callback_data="menu_rundown"),
            InlineKeyboardButton("💰 Saldo & Koin", callback_data="menu_koin")
        ],
        [
            InlineKeyboardButton("🎁 Angpao & Donasi", callback_data="menu_angpao"),
            InlineKeyboardButton("🎵 Music Request", callback_data="menu_musik")
        ],
        [
            InlineKeyboardButton("📸 Photobooth", callback_data="menu_photobooth"),
            InlineKeyboardButton("🍽️ Layanan Meja", callback_data="menu_layanan")
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
# START & MAIN ROUTER
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    now = int(time.time())

    # Clear state saat membuka menu utama
    context.user_data.clear()

    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO users (user_id, username, created_at) VALUES (?, ?, ?) 
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username""",
            (user_id, username, now)
        )
        await db.commit()

    text = (
        "✨ <b>ROYAL WEDDING ORGANIZER & EVENT SIMULATOR (RP)</b> ✨\n"
        "──────────────────────────────────────────\n"
        "<i>\"Mewujudkan Impian Pernikahan Pasangan Pengantin Tanpa Batas\"</i>\n\n"
        "Selamat datang di Sistem Manajemen Event! "
        "Fasilitas ini terintegrasi penuh dengan channel <b>@RoyalWeddingRP</b>.\n"
        "Semua fitur dapat diakses secara interaktif melalui tombol di bawah ini:"
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

    # Reset input state jika berpindah menu
    if not data.startswith("act_"):
        context.user_data['state'] = None

    if data == "menu_main":
        await start(update, context)

    # ----------------------------------------------------
    # MENU PAKET & PRICING
    # ----------------------------------------------------
    elif data == "menu_paket":
        text = "📋 <b>KATALOG PAKET & PRICING EVENT</b>\n\nPilih jenis paket untuk melihat rincian fasilitas & biaya:"
        keyboard = [
            [
                InlineKeyboardButton("Silver (500k)", callback_data="pkt_silver"),
                InlineKeyboardButton("Gold (1.2jt)", callback_data="pkt_gold"),
                InlineKeyboardButton("Platinum (2.5jt)", callback_data="pkt_platinum")
            ],
            [InlineKeyboardButton("📊 Perbandingan Paket", callback_data="pkt_banding")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ("pkt_silver", "pkt_gold", "pkt_platinum"):
        pkt_map = {
            "pkt_silver": ("SILVER — \"Simple & Sakral\"", "500.000 Koin", "• Venue 100 Tamu\n• Prasmanan 3 menu\n• Dekorasi Minimalis\n• MC + Sound Standard"),
            "pkt_gold": ("GOLD — \"Elegant Wedding\"", "1.200.000 Koin", "• Venue 250 Tamu\n• Prasmanan 5 menu + 2 Snack Corner\n• Dekorasi Custom\n• MC + Band Akustik\n• Foto + Video SDE"),
            "pkt_platinum": ("PLATINUM — \"Royal Celebration\"", "2.500.000 Koin", "• Venue 500+ Tamu (Indoor/Outdoor)\n• Full Course 8 Menu\n• Dekorasi Premium + Lighting\n• MC Bilingual + Orkestra\n• Full Dokumentasi (Drone)")
        }
        title, price, desc = pkt_map[data]
        text = f"💎 <b>PAKET {title}</b>\n💵 Biaya: <b>{price}</b>\n\n<b>Fasilitas Termasuk:</b>\n{desc}"
        keyboard = [
            [InlineKeyboardButton("🎉 Pesan Paket Ini", callback_data=f"act_pilih_pkt_{data.split('_')[1]}")],
            [InlineKeyboardButton("◀️ Kembali ke Katalog Paket", callback_data="menu_paket")]
        ]
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
            "+---------------+----------+----------+------------------"
            "</pre>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU BUAT EVENT (FULL BUTTON)
    # ----------------------------------------------------
    elif data == "menu_buat_event":
        text = "🎉 <b>PERENCANAAN & PEMBUATAN EVENT</b>\n\nPilih langkah operasional pembuatan acara:"
        keyboard = [
            [
                InlineKeyboardButton("➕ Buat Event Baru", callback_data="act_start_create_event"),
                InlineKeyboardButton("📅 Jadwal Terdaftar", callback_data="evt_jadwal")
            ],
            [
                InlineKeyboardButton("🌸 Katalog Tema Dekor", callback_data="evt_tema"),
                InlineKeyboardButton("💳 Bayar Tagihan Event", callback_data="act_bayar_event")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("act_pilih_pkt_") or data == "act_start_create_event":
        pkt = data.replace("act_pilih_pkt_", "") if "act_pilih_pkt_" in data else "gold"
        context.user_data['selected_paket'] = pkt
        context.user_data['state'] = "WAITING_EVENT_DETAILS"
        
        text = (
            f"📝 <b>FORMULIR PEMBUATAN EVENT ({pkt.upper()})</b>\n"
            "──────────────────────────\n"
            "Silakan ketik detail acara Anda dalam <b>SATU PESAN</b> dengan format berikut:\n\n"
            "<code>Nama Pria, Umur, Nama Ortu Pria | Nama Wanita, Umur, Nama Ortu Wanita | Tgl & Jam | Lokasi | Est.Tamu</code>\n\n"
            "<i>Contoh:</i>\n"
            "<code>Sora Pratama, 26, Bp. Hendra | Hana Amelia, 24, Bp. Wijaya | 20-12-2026 10:00 WIB | Grand Ballroom | 300</code>"
        )
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_jadwal":
        async with get_db_connection() as db:
            async with db.execute("SELECT event_id, mempelai_pria_nama, mempelai_wanita_nama, tgl_jam, status FROM events ORDER BY event_id DESC LIMIT 5") as cursor:
                rows = await cursor.fetchall()
        
        list_evt = ""
        if rows:
            for eid, p, w, t, st in rows:
                list_evt += f"• <b>Event #{eid}:</b> {p} & {w} ({t}) — [{st}]\n"
        else:
            list_evt = "<i>Belum ada event terdaftar.</i>"

        text = f"📅 <b>JADWAL EVENT TERDAFTAR</b>\n\n{list_evt}"
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

    elif data == "act_bayar_event":
        async with get_db_connection() as db:
            async with db.execute("SELECT event_id, total_biaya FROM events WHERE host_user_id = ? AND status = 'Pending' ORDER BY event_id DESC LIMIT 1", (user_id,)) as cursor:
                event = await cursor.fetchone()

            if not event:
                return await query.edit_message_text("❌ Anda tidak memiliki tagihan event yang bernilai Pending.", reply_markup=InlineKeyboardMarkup([[btn_back()]]))

            event_id, total_biaya = event
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                user_row = await cursor.fetchone()
                user_koin = user_row[0] if user_row else 0

            if user_koin < total_biaya:
                return await query.edit_message_text(f"❌ Saldo Koin Anda tidak mencukupi!\nBiaya: <b>{total_biaya:,} Koin</b>\nSaldo Anda: <b>{user_koin:,} Koin</b>", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

            await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (total_biaya, user_id))
            await db.execute("UPDATE events SET status = 'Paid' WHERE event_id = ?", (event_id,))
            await log_koin_transaction(db, user_id, -total_biaya, "PAYMENT_EVENT", f"Pembayaran Booking Event #{event_id}")
            await db.commit()

        await query.edit_message_text(f"✅ <b>PEMBAYARAN SUCCESS!</b>\nEvent #{event_id} telah dilunasi senilai <b>{total_biaya:,} Koin</b>.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU VENDOR & ROLE MC
    # ----------------------------------------------------
    elif data == "menu_vendor":
        text = "🧑‍🤝‍🧑 <b>DIREKTORI VENDOR & ROLEPLAY MC</b>\n\nPilih aksi operasional vendor:"
        keyboard = [
            [InlineKeyboardButton("🎤 Panggil MC (Prosesi)", callback_data="ven_mc_select")],
            [InlineKeyboardButton("🍽️ Layanan Catering", callback_data="act_catering_prompt"), InlineKeyboardButton("🥤 Layanan Bar/Minuman", callback_data="act_minum_prompt")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ven_mc_select":
        text = "🎤 <b>PILIH NARRASI MC UNTUK PROSESI PESTA:</b>"
        keyboard = [
            [InlineKeyboardButton("📢 Pembukaan Acara", callback_data="mc_buka"), InlineKeyboardButton("💍 Procession Ijab Qabul", callback_data="mc_ijab")],
            [InlineKeyboardButton("👑 Sambutan Resepsi", callback_data="mc_resepsi"), InlineKeyboardButton("🕯️ Sesi Sungkeman", callback_data="mc_sungkem")],
            [InlineKeyboardButton("🎉 Penutupan Acara", callback_data="mc_tutup")],
            [InlineKeyboardButton("◀️ Kembali", callback_data="menu_vendor")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ("mc_buka", "mc_ijab", "mc_resepsi", "mc_sungkem", "mc_tutup"):
        mc_texts = {
            "mc_buka": "🎤 <b>[MC ANNOUNCEMENT]</b>\n<i>\"Selamat datang bapak/ibu serta para tamu undangan sekalian di acara perayaan pernikahan yang penuh kebahagiaan ini...\"</i>",
            "mc_ijab": "🎤 <b>[MC ANNOUNCEMENT]</b>\n<i>\"Momen sakral dan khidmat... Prosesi Ijab Qabul akan segera dimulai. Hadirin dimohon mengikutinya dengan penuh kekhusyukan...\"</i>",
            "mc_resepsi": "🎤 <b>[MC ANNOUNCEMENT]</b>\n<i>\"Sambutlah kedua mempelai pengantin yang bersanding di pelaminan bagaikan Raja dan Ratu semalam!\"</i>",
            "mc_sungkem": "🎤 <b>[MC ANNOUNCEMENT]</b>\n<i>\"Momen haru penuh rasa syukur, kedua mempelai memohon doa restu kepada kedua orang tua tercinta...\"</i>",
            "mc_tutup": "🎤 <b>[MC ANNOUNCEMENT]</b>\n<i>\"Rangkaian acara telah usai. Kami mengucapkan terima kasih atas kehadiran dan doa restu Anda sekalian!\"</i>"
        }
        msg_text = mc_texts[data]
        await send_to_channel(context, text=msg_text)
        await query.edit_message_text(f"✅ Narasi MC telah disiarkan ke {CHANNEL_ID}:\n\n{msg_text}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="ven_mc_select")]]), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU PHOTOBOOTH (FIXED & IMPROVED IMAGE UPLOAD)
    # ----------------------------------------------------
    elif data == "menu_photobooth":
        text = (
            "📸 <b>DIGITAL PHOTOBOOTH & GALLERY CHANNEL</b>\n"
            "──────────────────────────\n"
            "Unggah foto gaya/pose pesta Anda! Foto akan secara otomatis dipublikasikan langsung ke channel <b>@RoyalWeddingRP</b>."
        )
        keyboard = [
            [InlineKeyboardButton("📷 Upload Foto Sekarang", callback_data="act_upload_photo")],
            [InlineKeyboardButton("🖼️ Lihat Galeri Foto", callback_data="act_view_gallery")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_upload_photo":
        context.user_data['state'] = "WAITING_PHOTOBOOTH_IMAGE"
        text = (
            "📸 <b>UPLOAD FOTO PHOTOBOOTH</b>\n\n"
            "Silakan <b>KIRIMKAN GAMBAR/FOTO</b> Anda ke chat bot ini sekarang!\n"
            "<i>(Anda juga bisa memberikan caption/ucapan pada foto yang dikirimkan)</i>"
        )
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_photobooth")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_view_gallery":
        async with get_db_connection() as db:
            async with db.execute("SELECT username, caption, created_at FROM photo_gallery WHERE event_id = 1 ORDER BY id DESC LIMIT 5") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            gal_text = "<i>Belum ada foto terunggah di galeri.</i>"
        else:
            gal_text = ""
            for u, c, t in rows:
                gal_text += f"• @{u}: <i>\"{c}\"</i>\n"

        text = f"🖼️ <b>GALERI PHOTOBOOTH TERAKHIR</b>\n\n{gal_text}"
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_photobooth")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU ANGPAO & DONASI (AUTO SEND CHANNEL)
    # ----------------------------------------------------
    elif data == "menu_angpao":
        text = (
            "🎁 <b>ANGPAO & DONASI PESTA PERNIKAHAN</b>\n"
            "──────────────────────────\n"
            "Kirimkan hadiah koin & doa restu kepada mempelai pengantin.\n"
            "Angpao yang dikirimkan akan disiarkan langsung ke channel <b>@RoyalWeddingRP</b>!"
        )
        keyboard = [
            [
                InlineKeyboardButton("🎁 50.000 Koin", callback_data="act_angpao_50000"),
                InlineKeyboardButton("🎁 100.000 Koin", callback_data="act_angpao_100000")
            ],
            [
                InlineKeyboardButton("🎁 250.000 Koin", callback_data="act_angpao_250000"),
                InlineKeyboardButton("🎁 500.000 Koin", callback_data="act_angpao_500000")
            ],
            [
                InlineKeyboardButton("✏️ Custom Nominal Angpao", callback_data="act_angpao_custom")
            ],
            [
                InlineKeyboardButton("📖 Lihat Buku Tamu & Doa", callback_data="act_buku_tamu"),
                InlineKeyboardButton("💎 Klaim Doorprize", callback_data="act_claim_doorprize_prompt")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("act_angpao_"):
        val_str = data.replace("act_angpao_", "")
        if val_str == "custom":
            context.user_data['state'] = "WAITING_ANGPAO_CUSTOM_AMOUNT"
            text = "✏️ Ketikkan nominal Koin angpao/donasi yang ingin Anda berikan:"
            return await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Batal", callback_data="menu_angpao")]]))
        
        amount = int(val_str)
        context.user_data['angpao_amount'] = amount
        context.user_data['state'] = "WAITING_ANGPAO_MESSAGE"
        
        text = f"✉️ <b>NILAI ANGPAO: {amount:,} KOIN</b>\n\nSilakan ketikkan <b>pesan/doa restu</b> Anda untuk pasangan pengantin:"
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_angpao")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_buku_tamu":
        async with get_db_connection() as db:
            async with db.execute("SELECT from_username, jumlah_koin, pesan FROM angpao WHERE event_id = 1 ORDER BY id DESC LIMIT 10") as cursor:
                rows = await cursor.fetchall()
        if not rows:
            bt_text = "<i>Belum ada pesan ucapan di buku tamu.</i>"
        else:
            bt_text = ""
            for u, k, m in rows:
                bt_text += f"• <b>@{u}</b> (🎁 {k:,} Koin): <i>\"{m}\"</i>\n"

        text = f"📖 <b>BUKU TAMU & HARAPAN PENGANTIN</b>\n\n{bt_text}"
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_angpao")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_claim_doorprize_prompt":
        context.user_data['state'] = "WAITING_CLAIM_AMOUNT"
        text = "💎 <b>KLAIM HADIAH DOORPRIZE / ANGPAO</b>\n\nKetik nominal koin klaim yang ingin diajukan ke Admin:"
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_angpao")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU TAMU & RSVP
    # ----------------------------------------------------
    elif data == "menu_rsvp":
        async with get_db_connection() as db:
            async with db.execute("SELECT username, status_rsvp, meja FROM rsvp WHERE event_id = 1 ORDER BY id DESC LIMIT 8") as cursor:
                rows = await cursor.fetchall()

        list_tamu = ""
        if rows:
            for uname, st, meja in rows:
                icon = "✅" if st == "hadir" else "❌"
                list_tamu += f"• @{uname} — {icon} <b>{st.upper()}</b> (Meja: {meja})\n"
        else:
            list_tamu = "<i>Belum ada konfirmasi kehadiran.</i>"

        text = f"💌 <b>KONFIRMASI KEHADIRAN (RSVP)</b>\n\n<b>Daftar Tamu Terakhir:</b>\n{list_tamu}"
        keyboard = [
            [
                InlineKeyboardButton("✅ Hadir Pesta", callback_data="act_rsvp_hadir"),
                InlineKeyboardButton("❌ Halangan Hadir", callback_data="act_rsvp_tidak")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ("act_rsvp_hadir", "act_rsvp_tidak"):
        st_val = "hadir" if data == "act_rsvp_hadir" else "tidak"
        now = int(time.time())
        async with get_db_connection() as db:
            await db.execute("INSERT INTO rsvp (event_id, user_id, username, status_rsvp, created_at) VALUES (1, ?, ?, ?, ?)", (user_id, username, st_val, now))
            await db.commit()

        await query.edit_message_text(
            f"💌 Terima kasih @{username}! Status Kehadiran RSVP Anda tercatat: <b>{st_val.upper()}</b>.",
            reply_markup=InlineKeyboardMarkup([[btn_back()]]),
            parse_mode="HTML"
        )

    # ----------------------------------------------------
    # MENU MUSIC REQUEST
    # ----------------------------------------------------
    elif data == "menu_musik":
        text = "🎵 <b>REQUEST LAGU KE BAND PESTA</b>\n\nPilih opsi musik pesta di bawah ini:"
        keyboard = [
            [InlineKeyboardButton("🎼 Request Judul Lagu", callback_data="act_req_lagu_prompt")],
            [InlineKeyboardButton("📜 Lihat Antrean Daftar Lagu", callback_data="act_list_lagu")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_req_lagu_prompt":
        context.user_data['state'] = "WAITING_SONG_NAME"
        text = "🎼 Ketikkan <b>Judul Lagu & Nama Penyanyi</b> yang ingin dimainkan Band:"
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_musik")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_list_lagu":
        async with get_db_connection() as db:
            async with db.execute("SELECT username, judul_lagu, status FROM song_requests WHERE event_id = 1 ORDER BY id DESC LIMIT 10") as cursor:
                rows = await cursor.fetchall()
        if not rows:
            music_list = "<i>Antrean lagu masih kosong.</i>"
        else:
            music_list = ""
            for u, l, s in rows:
                music_list += f"• <b>{l}</b> (req: @{u}) — [{s}]\n"

        text = f"🎵 <b>DAFTAR ANTREAN LAGU BAND</b>\n\n{music_list}"
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_musik")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU SALDO & KOIN
    # ----------------------------------------------------
    elif data == "menu_koin":
        async with get_db_connection() as db:
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                koin = row[0] if row else 0

        text = (
            "💰 <b>SALDO KOIN & KEUANGAN</b>\n"
            "──────────────────────────\n"
            f"• <b>Saldo Anda :</b> <b>{koin:,} Koin</b>\n\n"
            "Gunakan tombol di bawah ini untuk klaim tunjangan harian atau kirim koin:"
        )
        keyboard = [
            [InlineKeyboardButton("💵 Klaim Koin Harian (+50k)", callback_data="act_daily_koin")],
            [InlineKeyboardButton("💸 Transfer Koin ke User", callback_data="act_transfer_prompt")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "act_daily_koin":
        now = int(time.time())
        async with get_db_connection() as db:
            async with db.execute("SELECT last_daily FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                last = row[0] if row and row[0] else 0

            if now - last < 86400:
                sisa_jam = round((86400 - (now - last)) / 3600, 1)
                return await query.edit_message_text(f"⏳ Tunjangan harian sudah diklaim!\nSilakan coba lagi dalam <b>{sisa_jam} jam</b>.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

            await db.execute("UPDATE users SET koin = koin + 50000, last_daily = ? WHERE user_id = ?", (now, user_id))
            await log_koin_transaction(db, user_id, 50000, "DAILY_BONUS", "Klaim Tunjangan Harian")
            await db.commit()

        await query.edit_message_text("💵 <b>KLAIM SUCCESS!</b>\nAnda mendapatkan bonus harian <b>+50.000 Koin</b>.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

    elif data == "act_transfer_prompt":
        context.user_data['state'] = "WAITING_TRANSFER_DATA"
        text = "💸 Ketikkan transfer dengan format:\n<code>[User_ID_Tujuan] [Jumlah_Koin]</code>\n\n<i>Contoh:</i>\n<code>12345678 100000</code>"
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_koin")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU LAYANAN MEJA & CATERING
    # ----------------------------------------------------
    elif data == "menu_layanan":
        text = "🍽️ <b>LAYANAN MAKANAN & MINUMAN MEJA GRUP</b>\n\nPilih layanan pramusaji:"
        keyboard = [
            [InlineKeyboardButton("🍽️ Pesan Makanan", callback_data="act_catering_prompt"), InlineKeyboardButton("🥤 Pesan Minuman", callback_data="act_minum_prompt")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data in ("act_catering_prompt", "act_minum_prompt"):
        is_food = (data == "act_catering_prompt")
        context.user_data['state'] = "WAITING_CATERING_ORDER" if is_food else "WAITING_DRINK_ORDER"
        item_type = "makanan" if is_food else "minuman"
        text = f"🍽️ Ketikkan menu {item_type} yang ingin Anda pesan dari pramusaji:"
        keyboard = [[InlineKeyboardButton("❌ Batal", callback_data="menu_layanan")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # RUNDOWN, LAPORAN & BANTUAN
    # ----------------------------------------------------
    elif data == "menu_rundown":
        text = (
            "⏱️ <b>RUNDOWN ACARA PESTA PERNIKAHAN</b>\n"
            "──────────────────────────\n"
            "• 08.00 WIB — Registrasi & Penyambutan Tamu\n"
            "• 09.00 WIB — Procession Akad Nikah & Ijab Qabul\n"
            "• 11.00 WIB — Pembukaan Resepsi & Kirab Pengantin\n"
            "• 12.00 WIB — Ramah Tamah, Catering & Hiburan Band\n"
            "• 14.00 WIB — Pengundian Doorprize & Sesi Foto\n"
            "• 15.00 WIB — Penutupan Acara"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_laporan":
        text = "📊 <b>LAPORAN EVENT ORGANIZER</b>\n\nAcara simulasi berjalan lancar dengan indeks kepuasan tamu ⭐⭐⭐⭐⭐ (5.0/5.0)."
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "menu_bantuan":
        text = (
            "❓ <b>PANDUAN OPERASIONAL BOT</b>\n\n"
            "• Gunakan semua tombol interaktif untuk mengontrol event.\n"
            "• Foto Photobooth & Angpao akan otomatis terkirim ke <b>@RoyalWeddingRP</b>.\n"
            "• Jika mengalami kendala, hubungi Admin melalui menu Admin Panel."
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # ----------------------------------------------------
    # MENU ADMIN PANEL (WITH BUTTON APPROVALS)
    # ----------------------------------------------------
    elif data == "menu_admin":
        async with get_db_connection() as db:
            tier = await check_admin_tier(db, user_id)

        if tier < 1:
            return await query.edit_message_text("🚫 <b>AKSES DITOLAK:</b> Fitur ini khusus Admin.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

        text = (
            f"⚙️ <b>ADMIN CONTROL PANEL (Tier {tier})</b>\n"
            "──────────────────────────\n"
            "Kelola persetujuan klaim dan acara melalui tombol di bawah:"
        )
        keyboard = [
            [InlineKeyboardButton("📋 Kelola Klaim Pending", callback_data="adm_list_klaim")],
            [InlineKeyboardButton("🎲 Undi Doorprize Tamu", callback_data="adm_draw_doorprize")],
            [InlineKeyboardButton("🔄 Reset Event Data", callback_data="adm_reset_event")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "adm_list_klaim":
        async with get_db_connection() as db:
            async with db.execute("SELECT claim_id, username, tipe_klaim, nominal FROM claims WHERE status = 'Pending' LIMIT 5") as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await query.edit_message_text("📋 Tidak ada antrean klaim pending.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="menu_admin")]]))

        keyboard = []
        lines = ["📋 <b>DAFTAR KLAIM PENDING</b>\n"]
        for cid, uname, tipe, nom in rows:
            lines.append(f"• ID #{cid} | @{uname} | {tipe} | <b>{nom:,} Koin</b>")
            keyboard.append([
                InlineKeyboardButton(f"✅ Approve #{cid}", callback_data=f"adm_app_{cid}"),
                InlineKeyboardButton(f"❌ Reject #{cid}", callback_data=f"adm_rej_{cid}")
            ])
        keyboard.append([InlineKeyboardButton("◀️ Kembali", callback_data="menu_admin")])
        await query.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data.startswith("adm_app_") or data.startswith("adm_rej_"):
        cid = int(data.split("_")[2])
        is_approve = "adm_app_" in data
        now = int(time.time())

        async with get_db_connection() as db:
            async with db.execute("SELECT user_id, tipe_klaim, nominal, status FROM claims WHERE claim_id = ?", (cid,)) as cursor:
                claim = await cursor.fetchone()

            if not claim or claim[3] != 'Pending':
                return await query.edit_message_text("❌ Klaim ini sudah diproses atau tidak ditemukan.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="adm_list_klaim")]]))

            t_id, tipe, nom, _ = claim

            if is_approve:
                await db.execute("UPDATE claims SET status = 'Approved', admin_id = ?, processed_at = ? WHERE claim_id = ?", (user_id, now, cid))
                await db.execute("UPDATE users SET koin = koin + ? WHERE user_id = ?", (nom, t_id))
                await log_koin_transaction(db, t_id, nom, "CLAIM_APPROVED", f"Klaim {tipe} #{cid} disetujui admin")
                await db.commit()
                res_text = f"✅ Klaim #{cid} disetujui. Saldo <b>{nom:,} Koin</b> telah dikirim ke user."
            else:
                await db.execute("UPDATE claims SET status = 'Rejected', admin_id = ?, processed_at = ? WHERE claim_id = ?", (user_id, now, cid))
                await db.commit()
                res_text = f"❌ Klaim #{cid} telah ditolak."

        await query.edit_message_text(res_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali ke Klaim", callback_data="adm_list_klaim")]]), parse_mode="HTML")

    elif data == "adm_draw_doorprize":
        async with get_db_connection() as db:
            async with db.execute("SELECT username FROM rsvp WHERE event_id = 1 AND status_rsvp = 'hadir'") as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return await query.edit_message_text("🎲 Belum ada tamu hadir untuk diundi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="menu_admin")]]))

        winner = random.choice(rows)[0]
        broadcast_msg = f"🎲 <b>LUCKY DRAW WINNER (@RoyalWeddingRP)</b>\n🏆 Selamat Kepada <b>@{winner}</b> memenangkan Doorprize Utama Pesta!"
        await send_to_channel(context, text=broadcast_msg)
        await query.edit_message_text(f"🎲 <b>Pemenang Undian:</b> @{winner}!\nHasil telah disiarkan ke channel.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="menu_admin")]), parse_mode="HTML"])

    elif data == "adm_reset_event":
        async with get_db_connection() as db:
            await db.execute("DELETE FROM events")
            await db.execute("DELETE FROM rsvp")
            await db.execute("DELETE FROM angpao")
            await db.execute("DELETE FROM claims")
            await db.commit()
        await query.edit_message_text("🔄 Data event & registrasi telah berhasil di-reset.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Kembali", callback_data="menu_admin")]]))

# ==========================================
# MESSAGE HANDLERS (TEXT & PHOTO INPUTS)
# ==========================================
async def handle_photo_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani pengiriman foto untuk Photobooth & posting otomatis ke Channel."""
    state = context.user_data.get('state')
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    caption = update.message.caption or "Pose Cantik di Resepsi Wedding RP ✨"
    now = int(time.time())

    # Ambil file_id dari foto resolusi tertinggi
    photo_file_id = update.message.photo[-1].file_id

    # Kirim Gambar & Caption Langsung ke Channel Telegram
    channel_text = f"📸 <b>PHOTOBOOTH FEED (@RoyalWeddingRP)</b>\n👤 Tamu: <b>@{username}</b>\n💬 <i>\"{caption}\"</i>"
    ch_msg_id = await send_to_channel(context, text=channel_text, photo_file_id=photo_file_id)

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO photo_gallery (event_id, user_id, username, file_id, caption, channel_msg_id, created_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
            (user_id, username, photo_file_id, caption, ch_msg_id, now)
        )
        await db.commit()

    context.user_data['state'] = None
    await update.message.reply_text(
        f"📸 <b>FOTO PHOTOBOOTH BERHASIL DIUNGGAH!</b>\n\nFoto Anda telah dipublikasikan secara otomatis ke channel <b>{CHANNEL_ID}</b>.",
        reply_markup=InlineKeyboardMarkup([[btn_back()]]),
        parse_mode="HTML"
    )

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Menangani masukan teks berdasarkan alur/state tombol pengguna."""
    state = context.user_data.get('state')
    if not state:
        return

    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    text_input = update.message.text.strip()
    now = int(time.time())

    # 1. State Input Detail Event Baru
    if state == "WAITING_EVENT_DETAILS":
        parts = [p.strip() for p in text_input.split("|")]
        if len(parts) < 5:
            return await update.message.reply_text("❌ Format salah! Harap masukkan data dipisahkan tanda `|` sesuai petunjuk.")

        pkt = context.user_data.get('selected_paket', 'gold')
        biaya = PAKET_PRICING.get(pkt, 1200000)

        pria_raw, wanita_raw, tgl, lokasi, est_tamu = parts[0:5]
        p_parts = [x.strip() for x in pria_raw.split(",")]
        w_parts = [x.strip() for x in wanita_raw.split(",")]

        p_nama = p_parts[0] if len(p_parts) > 0 else "Pria"
        p_umur = int(p_parts[1]) if len(p_parts) > 1 and p_parts[1].isdigit() else 25
        p_ortu = p_parts[2] if len(p_parts) > 2 else "Keluarga"

        w_nama = w_parts[0] if len(w_parts) > 0 else "Wanita"
        w_umur = int(w_parts[1]) if len(w_parts) > 1 and w_parts[1].isdigit() else 23
        w_ortu = w_parts[2] if len(w_parts) > 2 else "Keluarga"

        est_num = int(est_tamu) if est_tamu.isdigit() else 100

        # Broadcast Pengumuman Event ke Channel
        ch_text = (
            f"📢 <b>NEW WEDDING EVENT REGISTERED!</b>\n"
            "──────────────────────────\n"
            f"📦 <b>Paket        :</b> Paket {pkt.upper()} ({biaya:,} Koin)\n"
            f"🤵 <b>Mempelai Pria:</b> {p_nama} ({p_umur} Thn)\n"
            f"👰 <b>Mempelai Wn :</b> {w_nama} ({w_umur} Thn)\n"
            "──────────────────────────\n"
            f"📍 <b>Lokasi       :</b> {lokasi}\n"
            f"📅 <b>Waktu        :</b> {tgl}\n"
            "──────────────────────────\n"
            "<i>Konfirmasi kehadiran Anda melalui tombol RSVP di bot!</i>"
        )
        ch_id = await send_to_channel(context, text=ch_text)

        async with get_db_connection() as db:
            await db.execute(
                """INSERT INTO events (host_user_id, jenis_event, paket, mempelai_pria_nama, mempelai_pria_umur, mempelai_pria_ortu,
                                     mempelai_wanita_nama, mempelai_wanita_umur, mempelai_wanita_ortu, tgl_jam, lokasi, est_tamu, status, total_biaya, channel_msg_id, created_at)
                   VALUES (?, 'Pernikahan', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?, ?)""",
                (user_id, pkt, p_nama, p_umur, p_ortu, w_nama, w_umur, w_ortu, tgl, lokasi, est_num, biaya, ch_id, now)
            )
            await db.commit()

        context.user_data['state'] = None
        await update.message.reply_text(
            f"🎉 <b>EVENT BERHASIL DIBUAT!</b>\n\nAcara pernikahan berhasil didaftarkan dengan Paket <b>{pkt.upper()}</b> dan disiarkan ke channel.",
            reply_markup=InlineKeyboardMarkup([[btn_back()]]),
            parse_mode="HTML"
        )

    # 2. State Input Pesan Angpao
    elif state == "WAITING_ANGPAO_MESSAGE":
        amount = context.user_data.get('angpao_amount', 50000)
        pesan = text_input

        async with get_db_connection() as db:
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                bal = row[0] if row else 0

            if bal < amount:
                context.user_data['state'] = None
                return await update.message.reply_text("❌ Saldo Koin Anda tidak mencukupi untuk memberikan angpao ini.")

            # Potong Saldo Koin
            await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (amount, user_id))
            await log_koin_transaction(db, user_id, -amount, "ANGPAO_GIVE", f"Kirim Angpao {amount} Koin")

            # Disiarkan ke Channel
            ch_text = (
                f"🎁 <b>ANGPAO & WISHING BROADCAST</b>\n"
                "──────────────────────────\n"
                f"✉️ <b>Dari:</b> @{username}\n"
                f"💰 <b>Nominal:</b> {amount:,} Koin\n"
                f"🕊️ <b>Doa Restu:</b> <i>\"{pesan}\"</i>"
            )
            ch_msg_id = await send_to_channel(context, text=ch_text)

            await db.execute(
                "INSERT INTO angpao (event_id, from_user_id, from_username, tipe, jumlah_koin, pesan, channel_msg_id, created_at) VALUES (1, ?, ?, 'Angpao', ?, ?, ?, ?)",
                (user_id, username, amount, pesan, ch_msg_id, now)
            )
            await db.commit()

        context.user_data['state'] = None
        await update.message.reply_text(
            f"🎁 <b>ANGPAO TERKIRIM!</b>\n\nAngpao sebesar <b>{amount:,} Koin</b> telah dikirimkan ke mempelai dan dipublikasikan ke channel.",
            reply_markup=InlineKeyboardMarkup([[btn_back()]]),
            parse_mode="HTML"
        )

    # 3. State Nominal Angpao Custom
    elif state == "WAITING_ANGPAO_CUSTOM_AMOUNT":
        if not text_input.isdigit() or int(text_input) <= 0:
            return await update.message.reply_text("❌ Harap masukkan angka nominal koin yang valid.")
        
        context.user_data['angpao_amount'] = int(text_input)
        context.user_data['state'] = "WAITING_ANGPAO_MESSAGE"
        await update.message.reply_text("✉️ Sekarang ketikkan <b>pesan/doa restu</b> Anda untuk pasangan pengantin:")

    # 4. State Klaim Doorprize
    elif state == "WAITING_CLAIM_AMOUNT":
        if not text_input.isdigit() or int(text_input) <= 0:
            return await update.message.reply_text("❌ Harap masukkan nominal klaim angka valid.")
        
        nom = int(text_input)
        async with get_db_connection() as db:
            await db.execute(
                "INSERT INTO claims (user_id, username, tipe_klaim, nominal, status, created_at) VALUES (?, ?, 'Doorprize', ?, 'Pending', ?)",
                (user_id, username, nom, now)
            )
            await db.commit()

        context.user_data['state'] = None
        await update.message.reply_text(f"📝 Pengajuan klaim Doorprize senilai <b>{nom:,} Koin</b> telah dikirimkan ke Admin untuk diverifikasi.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

    # 5. State Request Song
    elif state == "WAITING_SONG_NAME":
        async with get_db_connection() as db:
            await db.execute(
                "INSERT INTO song_requests (event_id, user_id, username, judul_lagu, status, created_at) VALUES (1, ?, ?, ?, 'Pending', ?)",
                (user_id, username, text_input, now)
            )
            await db.commit()

        context.user_data['state'] = None
        await update.message.reply_text(f"🎵 Request lagu <b>\"{text_input}\"</b> telah dimasukkan ke daftar antrean Band Pesta!", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

    # 6. State Transfer Koin
    elif state == "WAITING_TRANSFER_DATA":
        parts = text_input.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return await update.message.reply_text("❌ Format transfer salah! Ketik: `[User_ID] [Jumlah]`")

        target_id, amount = int(parts[0]), int(parts[1])
        async with get_db_connection() as db:
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                bal = row[0] if row else 0

            if bal < amount:
                context.user_data['state'] = None
                return await update.message.reply_text("❌ Saldo koin Anda tidak mencukupi.")

            await db.execute("UPDATE users SET koin = koin - ? WHERE user_id = ?", (amount, user_id))
            await db.execute("INSERT INTO users (user_id, koin) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET koin=koin+?", (target_id, amount, amount))
            await log_koin_transaction(db, user_id, -amount, "TRANSFER_OUT", f"Transfer ke user {target_id}")
            await log_koin_transaction(db, target_id, amount, "TRANSFER_IN", f"Transfer dari user {user_id}")
            await db.commit()

        context.user_data['state'] = None
        await update.message.reply_text(f"💸 Berhasil mentransfer <b>{amount:,} Koin</b> ke ID <code>{target_id}</code>.", reply_markup=InlineKeyboardMarkup([[btn_back()]]), parse_mode="HTML")

    # 7. State Order Makanan & Minuman
    elif state in ("WAITING_CATERING_ORDER", "WAITING_DRINK_ORDER"):
        is_food = (state == "WAITING_CATERING_ORDER")
        item_type = "makanan" if is_food else "minuman"
        
        context.user_data['state'] = None
        await update.message.reply_text(
            f"🍽️ Pramusaji mengantarkan pesanan {item_type} <b>\"{text_input}\"</b> khusus untuk <b>@{username}</b>. Selamat menikmati! ✨",
            reply_markup=InlineKeyboardMarkup([[btn_back()]]),
            parse_mode="HTML"
        )

# ==========================================
# BUILD APPLICATION & BOT LAUNCH
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(global_error_handler)

    # Command Handler Utama untuk Membuka Menu
    app.add_handler(CommandHandler("start", start))

    # Callback Query Router (Interaksi Tombol Keyboard)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Media & Text Handlers untuk Menerima Input Stateful Pengguna
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    return app

def main():
    asyncio.run(init_wedding_db())
    app = build_app()
    print("🤖 Bot Wedding Organizer & Event RP (@RoyalWeddingRP Linked) Running Successfully...")
    app.run_polling()

if __name__ == "__main__":
    main()
