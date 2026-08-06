"""
bot_launcher.py
================
Menjalankan semua bot Telegram (Operations, Vault, dan bot tambahan lain) dalam
SATU proses Python (satu event loop asyncio).

KENAPA INI PERLU:
Railway TIDAK mendukung shared volume/filesystem antar service. Kalau tiap bot
dijalankan sebagai service terpisah, masing-masing punya file `cosa_nostra.db`
sendiri-sendiri yang terisolasi -> data tidak pernah benar-benar sinkron walau
kodenya identik.

Dengan menjalankan semua bot di 1 proses (1 service Railway), semuanya otomatis
berbagi filesystem yang sama -> 1 file database fisik yang sama -> data selalu sinkron.

CARA NAMBAH BOT BARU LAGI (misal bot ke-4, ke-5, dst):
1. Bikin file bot baru, pastikan:
   - DB_NAME dihitung dari BASE_DIR/__file__ dengan pola yang sama seperti
     operation_bot.py, vault_bot_tele.py, & lineage_bot.py (biar otomatis
     nunjuk ke file db yang sama).
   - main() dipecah jadi build_app() yang cuma pasang handler & return app
     (JANGAN panggil app.run_polling() di dalam build_app()).
   - Baca token dari env var baru, misal TELEGRAM_XXX_BOT_TOKEN.
2. Import modulnya di bawah ini, lalu tambahkan ke dalam main() (lihat pola
   op_app / vault_app / lineage_app di bawah).
3. Tambah env var token barunya di Railway -> tab Variables (service yang sama,
   TIDAK perlu bikin service baru).
4. Push ke git seperti biasa -> Railway auto-redeploy.

CARA DEPLOY DI RAILWAY (setup awal):
1. Pastikan HANYA ADA 1 Railway service untuk semua bot ini.
2. Set Start Command service tsb ke:  python bot_launcher.py
3. Set semua environment variable token di service yang sama:
   - TELEGRAM_OPERATIONS_BOT_TOKEN
   - TELEGRAM_VAULT_BOT_TOKEN
   - TELEGRAM_LINEAGE_BOT_TOKEN
4. (Opsional tapi disarankan) Attach 1 Railway Volume ke service ini dan arahkan
   BASE_DIR/cosa_nostra.db ke path volume tsb, supaya data tidak hilang tiap redeploy
   (filesystem Railway tanpa volume bersifat ephemeral).
"""

import asyncio
import logging

import operation_bot
import vault_bot_tele
import lineage_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot_launcher")


async def start_bot(app, label: str):
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("%s started polling.", label)


async def stop_bot(app, label: str):
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("%s stopped cleanly.", label)
    except Exception:
        logger.exception("Error while stopping %s", label)


async def main():
    op_app = operation_bot.build_app()
    vault_app = vault_bot_tele.build_app()
    lineage_app = lineage_bot.build_app()

    await start_bot(op_app, "Operations Bot")
    await start_bot(vault_app, "Vault Bot")
    await start_bot(lineage_app, "Lineage Bot")

    logger.info("All bots are running in a single process, sharing the same database file.")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()  # jalan selamanya sampai proses dihentikan (SIGTERM dari Railway)
    finally:
        await stop_bot(op_app, "Operations Bot")
        await stop_bot(vault_app, "Vault Bot")
        await stop_bot(lineage_app, "Lineage Bot")


if __name__ == "__main__":
    asyncio.run(main())
