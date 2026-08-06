import asyncio
import logging
import signal
import aiosqlite  # pastikan aiosqlite di-import jika digunakan untuk inisialiasi

import operation_bot
import vault_bot_tele
import lineage_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot_launcher")

DB_PATH = "cosa_nostra.db"  # Sesuaikan dengan path database bersama Anda


async def init_shared_database():
    """Inisialisasi tabel bersama sebelum bot-bot dijalankan."""
    logger.info("Initializing shared database schema...")
    async with aiosqlite.connect(DB_PATH) as db:
        # Konfigurasi WAL mode untuk menghindari database locked
        await db.execute("PRAGMA journal_mode=WAL;")
        
        # Buat tabel users jika belum ada (mencegah error 'no such table')
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Tambahkan tabel lain jika diperlukan di sini...
        await db.commit()
    logger.info("Shared database schema initialized successfully.")


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
    # 1. Jalankan inisialisasi database bersama terlebih dahulu
    await init_shared_database()

    op_app = operation_bot.build_app()
    vault_app = vault_bot_tele.build_app()
    lineage_app = lineage_bot.build_app()

    await start_bot(op_app, "Operations Bot")
    await start_bot(vault_app, "Vault Bot")
    await start_bot(lineage_app, "Lineage Bot")

    logger.info("All bots are running in a single process, sharing the same database file.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Tangkap sinyal SIGINT (Ctrl+C) dan SIGTERM (Railway/Docker shutdown)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Penanganan untuk environment Windows jika dijalankan secara lokal
            pass

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Shutdown signal received. Stopping all bots...")
        await stop_bot(op_app, "Operations Bot")
        await stop_bot(vault_app, "Vault Bot")
        await stop_bot(lineage_app, "Lineage Bot")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot launcher terminated.")
