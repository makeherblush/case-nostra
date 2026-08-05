"""
bot_launcher.py
================
Menjalankan Operation Bot dan Vault Bot dalam SATU proses Python (satu event loop asyncio).

KENAPA INI PERLU:
Sebelumnya kedua bot dijalankan sebagai 2 Railway service terpisah. Railway TIDAK
mendukung shared volume/filesystem antar service (ini keterbatasan platform, bukan
bug di kode). Akibatnya masing-masing service punya file `cosa_nostra.db` sendiri-
sendiri yang terisolasi, sehingga koin/kepemilikan tidak pernah benar-benar sinkron
walau kodenya identik.

Dengan menjalankan keduanya di 1 proses (1 service Railway), kedua bot otomatis
berbagi filesystem yang sama -> 1 file database fisik yang sama -> data selalu sinkron.
SQLite WAL mode yang sudah dipakai di kedua file (`PRAGMA journal_mode=WAL`) memang
didesain untuk skenario multi-writer seperti ini dalam satu mesin/proses.

CARA DEPLOY DI RAILWAY:
1. Pastikan HANYA ADA 1 Railway service untuk kedua bot ini (hapus/nonaktifkan
   service kedua kalau sebelumnya terpisah).
2. Set Start Command service tsb ke:  python bot_launcher.py
3. Set kedua environment variable token di service yang sama:
   - TELEGRAM_OPERATIONS_BOT_TOKEN
   - TELEGRAM_VAULT_BOT_TOKEN
4. (Opsional tapi disarankan) Attach 1 Railway Volume ke service ini dan arahkan
   BASE_DIR/cosa_nostra.db ke path volume tsb, supaya data tidak hilang tiap redeploy
   (filesystem Railway tanpa volume bersifat ephemeral).
"""

import asyncio
import logging

import operation_bot
import vault_bot_tele

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

    await start_bot(op_app, "Operations Bot")
    await start_bot(vault_app, "Vault Bot")

    logger.info("Both bots are running in a single process, sharing the same database file.")

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()  # jalan selamanya sampai proses dihentikan (SIGTERM dari Railway)
    finally:
        await stop_bot(op_app, "Operations Bot")
        await stop_bot(vault_app, "Vault Bot")


if __name__ == "__main__":
    asyncio.run(main())
