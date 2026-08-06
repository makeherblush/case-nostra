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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DB_NAME = os.path.join(DB_DIR, "wedding_event.db")
WIB = timezone(timedelta(hours=7))  # UTC+7

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
        # Tabel Users & Koin RP
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                koin INTEGER DEFAULT 1000000
            )
        """)
        
        # Tabel Events
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_user_id INTEGER NOT NULL,
                jenis_event TEXT,
                nama_klien TEXT,
                tgl_jam TEXT,
                lokasi TEXT,
                est_tamu INTEGER,
                paket TEXT,
                tema TEXT,
                status TEXT DEFAULT 'Pending',
                total_biaya INTEGER DEFAULT 0,
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

        # TABEL BARU: Angpao Digital & Ucapan
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

        # TABEL BARU: Request Lagu
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

        # TABEL BARU: Galeri Photo Booth
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
# GLOBAL ERROR HANDLER
# ==========================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception occurred while handling an update: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ <b>TERJADI KENDALA OPERASIONAL!</b>\n\n"
            "Sistem event organizer mengalami masalah koneksi sementara. Silakan coba kembali perintah Anda.",
            parse_mode="HTML"
        )

# ==========================================
# KEYBOARD BUILDERS (NAVIGATION SCALFFOLDING)
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
            InlineKeyboardButton("🎁 Angpao & Wishing", callback_data="menu_angpao"),
            InlineKeyboardButton("🎵 Music Request", callback_data="menu_musik")
        ],
        [
            InlineKeyboardButton("📸 Photobooth", callback_data="menu_photobooth"),
            InlineKeyboardButton("📊 Laporan Acara", callback_data="menu_laporan")
        ],
        [
            InlineKeyboardButton("⚙️ Admin/GM", callback_data="menu_admin"),
            InlineKeyboardButton("❓ Bantuan", callback_data="menu_bantuan")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def btn_back():
    return InlineKeyboardButton("◀️ Kembali ke Menu Utama", callback_data="menu_main")

# ==========================================
# MAIN START COMMAND & CALLBACK ROUTER
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
        "<i>\"Mewujudkan Impian Pernikahan & Event Kemewahan Tanpa Batas\"</i>\n\n"
        "Selamat datang di Sistem Manajemen Event & Roleplay! "
        "Fasilitas ini mengontrol seluruh kebutuhan pesta, dari pemilihan paket, simulasi "
        "vendor, pengaturan rundown, hingga interaksi tamu secara real-time.\n\n"
        "Silakan pilih menu navigasi di bawah ini untuk memulai:"
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

    # --- ROOT NAVIGATION ---
    if data == "menu_main":
        await start(update, context)

    # --- 1. PAKET & HARGA ---
    elif data == "menu_paket":
        text = "📋 <b>KATALOG PAKET & PRICING EVENT</b>\n\nPilih jenis paket untuk melihat rincian fasilitas:"
        keyboard = [
            [
                InlineKeyboardButton("Silver", callback_data="pkt_silver"),
                InlineKeyboardButton("Gold", callback_data="pkt_gold"),
                InlineKeyboardButton("Platinum", callback_data="pkt_platinum")
            ],
            [
                InlineKeyboardButton("🎨 Custom (À la carte)", callback_data="pkt_custom"),
                InlineKeyboardButton("📊 Perbandingan Paket", callback_data="pkt_banding")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_silver":
        text = (
            "🤍 <b>PAKET SILVER — \"Simple & Sakral\"</b>\n"
            "💵 Biaya: <b>500.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 100 Tamu\n"
            "• Catering prasmanan 3 menu utama\n"
            "• Dekorasi minimalis 1 warna tema\n"
            "• MC + Sound System Standar\n"
            "• Dokumentasi Foto Only (1 Fotografer)\n"
            "• <i>Tanpa MUA (Bawa Mandiri)</i>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_gold":
        text = (
            "💛 <b>PAKET GOLD — \"Elegant Wedding\"</b>\n"
            "💵 Biaya: <b>1.200.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 250 Tamu\n"
            "• Catering Prasmanan 5 menu utama + 2 snack corner\n"
            "• Dekorasi tema custom (Pilih dari 5 tema)\n"
            "• MC + Band Akustik\n"
            "• Dokumentasi Foto + Video (Same Day Edit)\n"
            "• MUA + Busana Pengantin (1 riasan)\n"
            "• Among Tamu 2 orang"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_platinum":
        text = (
            "💎 <b>PAKET PLATINUM — \"Royal Celebration\"</b>\n"
            "💵 Biaya: <b>2.500.000 Koin</b>\n\n"
            "<b>Fasilitas Termasuk:</b>\n"
            "• Venue Kapasitas 500+ Tamu (Indoor + Outdoor)\n"
            "• Full course dinner 8 menu + 4 corner (Dessert, Coffee, Mocktail, Live Cooking)\n"
            "• Dekorasi Premium + Lighting Artistik\n"
            "• MC Bilingual + Live Band / Orkestra\n"
            "• Dokumentasi Lengkap (Foto, Video, Drone, SDE, Dokumenter)\n"
            "• MUA + Busana 3x ganti (Akad, Resepsi, Custom)\n"
            "• Among Tamu 5 orang + LO + Among Mobil\n"
            "• Souvenir Custom Nama"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_custom":
        text = (
            "🎨 <b>PAKET CUSTOM (À LA CARTE)</b>\n\n"
            "Bebas memilih vendor secara mandiri sesuai anggaran Anda!\n"
            "• Pilih Venue, Catering, MUA, dan Dokumentasi terpisah.\n"
            "• Bot akan menghitung total budget otomatis pada konfirmasi akhir."
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "pkt_banding":
        text = (
            "📊 <b>PERBANDINGAN FASILITAS PAKET</b>\n\n"
            "<pre>"
            "+---------------+----------+----------+------------------+\n"
            "| Fitur         | Silver   | Gold     | Platinum         |\n"
            "+---------------+----------+----------+------------------+\n"
            "| Kapasitas     | 100 Tamu | 250 Tamu | 500+ Tamu        |\n"
            "| Menu Utama    | 3        | 5        | 8                |\n"
            "| Dekorasi      | Minimalis| Custom   | Premium+Lighting |\n"
            "| Dokumentasi   | Foto     | Foto+SDE | Full+Drone       |\n"
            "| MUA           | ❌       | 1x       | 3x               |\n"
            "| Among Tamu    | ❌       | 2 Orang  | 5 Orang+LO       |\n"
            "+---------------+----------+----------+------------------"
            "</pre>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Paket", callback_data="menu_paket")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 2. BUAT EVENT ---
    elif data == "menu_buat_event":
        text = "🎉 <b>PERENCANAAN & PEMBUATAN EVENT</b>\n\nSilakan pilih langkah operasional pembuatan acara:"
        keyboard = [
            [
                InlineKeyboardButton("➕ Buat Acara Baru", callback_data="evt_baru"),
                InlineKeyboardButton("📅 Cek Tanggal Kosong", callback_data="evt_tanggal")
            ],
            [
                InlineKeyboardButton("🌸 Pilih Tema Dekor", callback_data="evt_tema"),
                InlineKeyboardButton("💼 Pilih Vendor", callback_data="menu_vendor")
            ],
            [
                InlineKeyboardButton("✅ Konfirmasi Booking", callback_data="evt_konfirmasi")
            ],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_baru":
        text = (
            "📝 <b>FORMULIR PEMBUATAN EVENT BARU</b>\n\n"
            "Silakan isi parameter acara melalui sintaks berikut:\n"
            "<code>/buat_event [Jenis] | [Nama Klien] | [Tgl&Jam] | [Lokasi] | [Est.Tamu]</code>\n\n"
            "<b>Pilihan Jenis Event:</b>\n"
            "• Pernikahan\n• Tunangan\n• Ulang Tahun\n• Gathering\n• Wisuda\n\n"
            "<i>Contoh:</i>\n"
            "<code>/buat_event Pernikahan | Axvel & Bara | 20-12-2026 10:00 | Ballroom | 300</code>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_tanggal":
        text = (
            "📅 <b>JADWAL & TANGGAL EVENT</b>\n\n"
            "• 15-12-2026 : 🟢 Kosong\n"
            "• 20-12-2026 : 🔴 Terisi (Pernikahan Axvel & Bara)\n"
            "• 25-12-2026 : 🟢 Kosong\n\n"
            "<i>Pilih tanggal yang masih berkode hijau!</i>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_tema":
        text = (
            "🌺 <b>KATALOG TEMA DEKORASI EVENT</b>\n\n"
            "🌸 <b>Rustic Garden:</b> Dominan hijau-putih, elemen kayu, bunga liar, lampu string.\n"
            "🕊️ <b>Modern Minimalist:</b> Monokrom (putih, abu, gold), garis bersih, backdrop geometris.\n"
            "🏛️ <b>Royal Classic:</b> Merah marun & gold, kursi tiffany, lampu kristal, karpet merah.\n"
            "🌿 <b>Tropical Bali:</b> Bunga kamboja, kain tenun, bambu, nuansa pantai.\n"
            "🎎 <b>Adat Nusantara:</b> Pilihan daerah (Jawa, Sunda, Batak, Bugis, Minang, dll).\n"
            "✨ <b>Enchanted Forest:</b> Nuansa gelap-emas, lilin, dedaunan, lampu gantung.\n"
            "💐 <b>Pastel Romance:</b> Pink pastel, lavender, bunga fresh, hiasan balon."
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "evt_konfirmasi":
        async with get_db_connection() as db:
            async with db.execute("SELECT jenis_event, nama_klien, tgl_jam, status, total_biaya FROM events WHERE host_user_id = ? ORDER BY event_id DESC LIMIT 1", (user_id,)) as cursor:
                row = await cursor.fetchone()

        if row:
            jenis, klien, tgl, status, biaya = row
            text = (
                "✅ <b>RINGKASAN & KONFIRMASI BOOKING</b>\n"
                "──────────────────────────\n"
                f"• <b>Status Event :</b> {status}\n"
                f"• <b>Jenis Acara   :</b> {jenis}\n"
                f"• <b>Nama Klien   :</b> {klien}\n"
                f"• <b>Tanggal      :</b> {tgl}\n"
                f"• <b>Total Est.    :</b> {biaya:,} Koin\n"
                "──────────────────────────\n"
                "<i>Gunakan /bayar_event untuk penyelesaian transaksi koin!</i>"
            )
        else:
            text = "❌ Belum ada draf event. Buat acara baru terlebih dahulu melalui tombol <b>Buat Acara Baru</b>."

        keyboard = [[InlineKeyboardButton("◀️ Kembali", callback_data="menu_buat_event")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 3. VENDOR & ROLE ---
    elif data == "menu_vendor":
        text = "🧑‍🤝‍🧑 <b>DIREKTORI VENDOR & OPERASIONAL ROLE</b>\n\nPilih kategori vendor untuk interaksi RP:"
        keyboard = [
            [InlineKeyboardButton("🎤 MC", callback_data="ven_mc"), InlineKeyboardButton("🍽️ Catering", callback_data="ven_catering")],
            [InlineKeyboardButton("🥤 Minuman", callback_data="ven_minuman"), InlineKeyboardButton("🎋 Dekorasi", callback_data="ven_dekor")],
            [InlineKeyboardButton("💄 MUA & Busana", callback_data="ven_mua"), InlineKeyboardButton("📸 Dokumentasi", callback_data="ven_dokum")],
            [InlineKeyboardButton("💃 Entertainment", callback_data="ven_entertain"), InlineKeyboardButton("🧑‍🤝‍🧑 Among Tamu", callback_data="ven_among")],
            [InlineKeyboardButton("🚗 Transportasi", callback_data="ven_trans"), InlineKeyboardButton("🎁 Souvenir", callback_data="ven_souvenir")],
            [btn_back()]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ven_mc":
        text = (
            "🎤 <b>ROLE MASTER OF CEREMONY (MC)</b>\n\n"
            "<b>Sesi Narasi RP Tersedia:</b>\n"
            "• <code>/mc_buka</code> — Pembukaan acara resmi\n"
            "• <code>/mc_ijab</code> — Memandu prosesi Ijab Qabul/Akad\n"
            "• <code>/mc_resepsi</code> — Sambutan resepsi & tamu\n"
            "• <code>/mc_sungkem</code> — Momen haru sungkeman\n"
            "• <code>/mc_tutup</code> — Penutupan & doa"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Vendor", callback_data="menu_vendor")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    elif data == "ven_catering":
        text = (
            "🍽️ <b>KATALOG CATERING & PRASMANAN</b>\n\n"
            "<b>Nusantara:</b> Nasi Kuning, Rendang Daging, Ayam Bakar Rujak, Sate, Sop Buntut, Gudeg.\n"
            "<b>Western:</b> Beef Steak Blackpepper, Chicken Cordon Bleu, Fish & Chips, Aglio Olio.\n"
            "<b>Oriental:</b> Kwetiau Seafood, Dimsum Platter, Fuyunghai.\n"
            "<b>Corners:</b> Dessert Corner, Coffee & Tea, Mocktail, Live Cooking Sate/Bakso.\n\n"
            "📊 <i>Status Saat Ini: [Sedang Disajikan]</i>"
        )
        keyboard = [[InlineKeyboardButton("◀️ Kembali ke Vendor", callback_data="menu_vendor")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 4. FITUR BARU: ANGPAO & WISHING ---
    elif data == "menu_angpao":
        text = (
            "🎁 <b>ANGPAO DIGITAL & BUKU TAMU DIGITAL</b>\n\n"
            "Beri dukungan koin dan doa terbaik Anda untuk penyelenggara acara!\n\n"
            "<b>Komando RP:</b>\n"
            "• <code>/angpao [jumlah_koin] | [pesan_doa]</code> — Kirim Amplop Angpao\n"
            "• <code>/bukutamu</code> — Lihat Daftar Ucapan & Doa Tamu"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 5. FITUR BARU: MUSIC REQUEST ---
    elif data == "menu_musik":
        text = (
            "🎵 <b>REQUEST LAGU KE ENTERTAINMENT / BAND</b>\n\n"
            "Minta lagu favorit Anda untuk dinyanyikan live oleh band di panggung!\n\n"
            "<b>Komando RP:</b>\n"
            "• <code>/request_lagu [Judul Lagu - Penyanyi]</code> — Ajukan Lagu\n"
            "• <code>/daftar_lagu</code> — Cek Queue Antrean Lagu"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 6. FITUR BARU: PHOTOBOOTH ---
    elif data == "menu_photobooth":
        text = (
            "📸 <b>DIGITAL PHOTO BOOTH & GALLERY</b>\n\n"
            "Abadikan momen anggun busana RP Anda di bilik foto pesta!\n\n"
            "<b>Komando RP:</b>\n"
            "• <code>/photobooth [link_foto/caption]</code> — Upload Foto Pose RP\n"
            "• <code>/gallery</code> — Lihat Galeri Foto Seluruh Tamu"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 7. TAMU & RSVP ---
    elif data == "menu_rsvp":
        async with get_db_connection() as db:
            async with db.execute("SELECT username, status_rsvp, kategori FROM rsvp WHERE event_id = 1") as cursor:
                rows = await cursor.fetchall()

        list_tamu = ""
        if rows:
            for uname, st, kat in rows:
                list_tamu += f"• @{uname} — <b>{st.capitalize()}</b> [{kat}]\n"
        else:
            list_tamu = "<i>Belum ada konfirmasi RSVP dari tamu.</i>"

        text = (
            "💌 <b>MANAJEMEN TAMU & KARTU RSVP</b>\n\n"
            "<b>Perintah Konfirmasi RSVP:</b>\n"
            "• <code>/rsvp hadir</code> — Konfirmasi Kehadiran\n"
            "• <code>/rsvp tidak</code> — Minta Maaf Halangan Hadir\n"
            "• <code>/rsvp mungkin</code> — Ragu-ragu\n\n"
            f"<b>Daftar Kehadiran Tamu:</b>\n{list_tamu}"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 8. RUNDOWN & STATE ---
    elif data == "menu_rundown":
        text = (
            "⏱️ <b>RUNDOWN & THRESHOLD STATE ACARA</b>\n\n"
            "<b>Status Acara Sekarang:</b> 🟢 <i>[Resepsi Berlangsung]</i>\n\n"
            "<b>Alur Tahapan Acara:</b>\n"
            "Persiapan ➔ Pembukaan ➔ Akad/Ijab ➔ <b>[Resepsi]</b> ➔ Hiburan ➔ Sungkeman ➔ Selesai\n\n"
            "⚠️ <i>Notifikasi Otomatis: Among Tamu bersiap menyambut Rombongan VIP!</i>"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 9. KOIN & TRANSAKSI ---
    elif data == "menu_koin":
        async with get_db_connection() as db:
            async with db.execute("SELECT koin FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                koin = row[0] if row else 0

        text = (
            "💰 <b>KOIN & SISTEM TRANSAKSI SIMULASI</b>\n\n"
            f"• <b>Saldo Anda :</b> <b>{koin:,} Koin</b>\n"
            "• <b>Status      :</b> VIP Customer\n\n"
            "<b>Gunakan Komando:</b>\n"
            "• <code>/transfer_koin [user_id] [jumlah]</code>\n"
            "• <code>/daily_koin</code> — Klaim koin harian"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 10. LAPORAN ACARA ---
    elif data == "menu_laporan":
        text = (
            "📊 <b>LAPORAN EXECUTIVE EVENT</b>\n\n"
            "• <b>Total Biaya Event :</b> 1.200.000 Koin\n"
            "• <b>Tamu Hadir         :</b> 210 / 250 Orang\n"
            "• <b>Rating Kepuasan    :</b> ⭐⭐⭐⭐⭐ (5.0)\n\n"
            "🏆 <i>Achievement Unlocked: \"Perfect Ceremony Master!\"</i>"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 11. ADMIN / GM TOOLS ---
    elif data == "menu_admin":
        text = (
            "⚙️ <b>ADMIN & GAME MASTER CONTROL PANEL</b>\n\n"
            "<b>Fitur Undian Doorprize:</b>\n"
            "• <code>/doorprize</code> — Undi Tamu Beruntung secara Acak!\n\n"
            "<b>Trigger Insiden Random (Bumbu Drama RP):</b>\n"
            "• <code>/incident hujan</code> — Turun Hujan Mendadak\n"
            "• <code>/incident catering_telat</code> — Catering Masih Dijalur\n"
            "• <code>/incident sound_dead</code> — Sound System Mati\n\n"
            "<b>Fitur Kontrol:</b>\n"
            "• <code>/broadcast [pesan]</code> — Kirim Pengumuman Pesta"
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    # --- 12. BANTUAN ---
    elif data == "menu_bantuan":
        text = (
            "❓ <b>PANDUAN OPERASIONAL & BANTUAN BOT</b>\n\n"
            "Bot ini dirancang khusus untuk memfasilitasi simulasi acara/pernikahan dalam konteks Roleplay (RP).\n\n"
            "• Gunakan tombol interaktif untuk menjelajahi katalog.\n"
            "• Gunakan command khusus di grup/chat pribadi untuk memicu aksi otomatis role tertentu (MC, MUA, Catering)."
        )
        keyboard = [[btn_back()]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

# ==========================================
# EXTRA COMMAND HANDLERS & BARU
# ==========================================
async def cmd_buat_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw_text = " ".join(context.args)

    if "|" not in raw_text:
        return await update.message.reply_text(
            "❌ <b>Format Salah!</b>\n\n"
            "Gunakan: <code>/buat_event [Jenis] | [Nama Klien] | [Tgl&Jam] | [Lokasi] | [Est.Tamu]</code>\n"
            "Contoh: <code>/buat_event Pernikahan | Axvel & Bara | 20-12-2026 10:00 | Ballroom | 300</code>",
            parse_mode="HTML"
        )

    parts = [p.strip() for p in raw_text.split("|")]
    if len(parts) < 5:
        return await update.message.reply_text("❌ Isian parameter kurang lengkap. Pastikan mengisi 5 bagian yang dipisahkan garis lurus <code>|</code>.", parse_mode="HTML")

    jenis, klien, tgl, lokasi, est_tamu = parts[:5]
    est_tamu_num = int(est_tamu) if est_tamu.isdigit() else 100
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO events (host_user_id, jenis_event, nama_klien, tgl_jam, lokasi, est_tamu, status, total_biaya, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'Confirmed', 1200000, ?)""",
            (user_id, jenis, klien, tgl, lokasi, est_tamu_num, now_epoch)
        )
        await db.commit()

    await update.message.reply_text(
        f"🎉 <b>EVENT BERHASIL DIDAFTARKAN!</b>\n\n"
        f"• <b>Jenis Event :</b> {jenis}\n"
        f"• <b>Klien       :</b> {klien}\n"
        f"• <b>Tanggal/Jam :</b> {tgl}\n"
        f"• <b>Lokasi      :</b> {lokasi}\n"
        f"• <b>Est. Tamu   :</b> {est_tamu_num} Orang\n\n"
        f"✨ <i>Status booking resmi diset menjadi <b>CONFIRMED</b>.</i>",
        parse_mode="HTML"
    )

async def cmd_rsvp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    
    if not context.args:
        return await update.message.reply_text("Format: <code>/rsvp [hadir|tidak|mungkin]</code>", parse_mode="HTML")

    st = context.args[0].lower()
    if st not in ("hadir", "tidak", "mungkin"):
        return await update.message.reply_text("❌ Pilihan status RSVP hanya: <code>hadir</code>, <code>tidak</code>, atau <code>mungkin</code>.", parse_mode="HTML")

    async with get_db_connection() as db:
        await db.execute(
            """INSERT INTO rsvp (event_id, user_id, username, status_rsvp)
               VALUES (1, ?, ?, ?)""",
            (user_id, username, st)
        )
        await db.commit()

    await update.message.reply_text(f"💌 Konfirmasi RSVP dari @{username} tercatat: <b>{st.upper()}</b>.", parse_mode="HTML")

# FITUR BARU: Angpao Digital
async def cmd_angpao(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    raw_text = " ".join(context.args)

    if "|" not in raw_text:
        return await update.message.reply_text("Format: <code>/angpao [jumlah_koin] | [pesan_doa]</code>\nContoh: <code>/angpao 50000 | Selamat menempuh hidup baru!</code>", parse_mode="HTML")

    parts = [p.strip() for p in raw_text.split("|", 1)]
    jumlah = int(parts[0]) if parts[0].isdigit() else 10000
    pesan = parts[1]
    now_epoch = int(time.time())

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO angpao (event_id, from_user_id, from_username, jumlah_koin, pesan, created_at) VALUES (1, ?, ?, ?, ?, ?)",
            (user_id, username, jumlah, pesan, now_epoch)
        )
        await db.commit()

    await update.message.reply_text(
        f"🎁 <b>ANGPAO & UCAPAN TERKIRIM!</b>\n\n"
        f"Dari: @{username}\n"
        f"Jumlah: <b>{jumlah:,} Koin</b>\n"
        f"Pesan: <i>\"{pesan}\"</i>",
        parse_mode="HTML"
    )

async def cmd_bukutamu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT from_username, jumlah_koin, pesan FROM angpao WHERE event_id = 1 ORDER BY id DESC LIMIT 10") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return await update.message.reply_text("📖 Buku tamu dan angpao masih kosong.", parse_mode="HTML")

    lines = ["📖 <b>BUKU TAMU & HARAPAN TERBAIK</b>\n"]
    for uname, koin, msg in rows:
        lines.append(f"• @{uname} (🎁 {koin:,} Koin): <i>\"{msg}\"</i>")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# FITUR BARU: Request Lagu
async def cmd_request_lagu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    judul = " ".join(context.args).strip()

    if not judul:
        return await update.message.reply_text("Format: <code>/request_lagu [Judul Lagu & Penyanyi]</code>", parse_mode="HTML")

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO song_requests (event_id, user_id, username, judul_lagu) VALUES (1, ?, ?, ?)",
            (user_id, username, judul)
        )
        await db.commit()

    await update.message.reply_text(f"🎵 Request lagu <b>\"{judul}\"</b> dari @{username} telah diteruskan ke Band Entertainment!", parse_mode="HTML")

async def cmd_daftar_lagu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT username, judul_lagu, status FROM song_requests WHERE event_id = 1 ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return await update.message.reply_text("🎵 Antrean request lagu masih kosong.", parse_mode="HTML")

    lines = ["🎵 <b>ANTEAN REQUEST LAGU LIVE BAND</b>\n"]
    for uname, lagu, st in rows:
        lines.append(f"• <b>{lagu}</b> (Request by @{uname}) — [{st}]")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# FITUR BARU: Photobooth
async def cmd_photobooth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name
    caption = " ".join(context.args).strip() or "Pose Anggun di Pesta"

    async with get_db_connection() as db:
        await db.execute(
            "INSERT INTO photo_gallery (event_id, user_id, username, photo_url, caption) VALUES (1, ?, ?, 'photo_placeholder', ?)",
            (user_id, username, caption)
        )
        await db.commit()

    await update.message.reply_text(f"📸 <b>PHOTOBOOTH SNAP!</b>\n\nFoto @{username} berhasil diabadikan ke Galeri Pesta!\nCaption: <i>\"{caption}\"</i>", parse_mode="HTML")

# FITUR BARU: Lucky Draw Doorprize
async def cmd_doorprize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with get_db_connection() as db:
        async with db.execute("SELECT username FROM rsvp WHERE event_id = 1 AND status_rsvp = 'hadir'") as cursor:
            rows = await cursor.fetchall()

    if not rows:
        return await update.message.reply_text("🎲 Belum ada tamu yang terkonfirmasi `hadir` untuk diundi.", parse_mode="HTML")

    winner = random.choice(rows)[0]
    await update.message.reply_text(
        f"🎲 <b>LUCKY DRAW DOORPRIZE PESTA!</b> 🎲\n\n"
        f"Mengundi seluruh tamu yang hadir...\n"
        f"🏆 <b>Pemenang Doorprize Utama:</b> @{winner}! Selamat! 🎉",
        parse_mode="HTML"
    )

async def cmd_mc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd = update.message.text.split()[0].replace("/", "")
    
    responses = {
        "mc_buka": "🎤 <i>\"Selamat datang Hadhirin sekalian dalam pesta kemewahan malam ini. Bersiaplah menyaksikan prosesi penyatuan yang sakral...\"</i>",
        "mc_ijab": "🎤 <i>\"Hadirin dimohon tenang, prosesi pengucapan janji sakral Ijab Qabul akan segera dimulai...\"</i>",
        "mc_resepsi": "🎤 <i>\"Memberi penghormatan setinggi-tingginya kepada kedua mempelai! Mari kita buka pesta resepsi ini dengan tepuk tangan meriah!\"</i>",
        "mc_sungkem": "🎤 <i>\"Suasana haru menyelimuti ruangan saat kedua mempelai bersimpuh memohon restu kepada kedua orang tua...\"</i>",
        "mc_tutup": "🎤 <i>\"Demikian rangkaian perayaan malam ini. Terimakasih atas kehadiran seluruh tamu undangan yang berbahagia!\"</i>"
    }

    text = responses.get(cmd, "🎤 MC siap memandu jalannya acara.")
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_incident(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("Format: <code>/incident [hujan|catering_telat|sound_dead]</code>", parse_mode="HTML")

    tipe = context.args[0].lower()
    incidents = {
        "hujan": "☔ <b>DRAMA EVENT:</b> Hujan deras mendadak turun! Tim Dekorasi berhamburan mengamankan area outdoor!",
        "catering_telat": "🍽️ <b>DRAMA EVENT:</b> Truk catering terjebak macet! Tamu VIP mulai mempertanyakan hidangan!",
        "sound_dead": "🎤 <b>DRAMA EVENT:</b> Sound system mendadak mati total saat prosesi utama berlangsung!"
    }

    text = incidents.get(tipe, "⚠️ Terjadi insiden tak terduga di lokasi pesta!")
    await update.message.reply_text(text, parse_mode="HTML")

# ==========================================
# BUILD APPLICATION & POLLING
# ==========================================
def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_error_handler(global_error_handler)

    # Navigation Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Event & RSVP Commands
    app.add_handler(CommandHandler("buat_event", cmd_buat_event))
    app.add_handler(CommandHandler("rsvp", cmd_rsvp))

    # Fitur Baru Commands
    app.add_handler(CommandHandler("angpao", cmd_angpao))
    app.add_handler(CommandHandler("bukutamu", cmd_bukutamu))
    app.add_handler(CommandHandler("request_lagu", cmd_request_lagu))
    app.add_handler(CommandHandler("daftar_lagu", cmd_daftar_lagu))
    app.add_handler(CommandHandler("photobooth", cmd_photobooth))
    app.add_handler(CommandHandler("doorprize", cmd_doorprize))

    # MC Role Commands
    app.add_handler(CommandHandler(["mc_buka", "mc_ijab", "mc_resepsi", "mc_sungkem", "mc_tutup"], cmd_mc))

    # GM Incident Command
    app.add_handler(CommandHandler("incident", cmd_incident))

    return app

def main():
    asyncio.run(init_wedding_db())
    app = build_app()
    print("🤖 Bot Wedding Organizer & Event RP (Advanced) Running...")
    app.run_polling()

if __name__ == "__main__":
    main()