from __future__ import annotations

import argparse
from contextlib import nullcontext
import sys
from pathlib import Path

from tokenote.config import DB_PATH, VERSION, ensure_data_dir
from tokenote.instance import SingleInstance
from tokenote.db import UsageDatabase
from tokenote.sync import SyncService


def run_sync(db_path: Path) -> int:
    db = UsageDatabase(db_path)
    service = SyncService(db)
    results = service.sync_all()
    for result in results:
        detail = f"imported={result.imported}, scanned={result.scanned}"
        if result.error:
            detail += f", error={result.error}"
        print(f"[{result.adapter}] {detail}")
    summary = db.summary()
    print(f"total={summary['total']} input={summary['input']} output={summary['output']} cache={summary['cache']} requests={summary['requests']}")
    return 0


def run_gui(db_path: Path, instance: SingleInstance | None = None) -> int:
    from tokenote.ui.app import TokenoteApp
    db = UsageDatabase(db_path)
    service = SyncService(db)
    app = TokenoteApp(db, service)
    try:
        if instance is not None:
            instance.watch(app)
        app.mainloop()
    finally:
        if instance is not None:
            instance.unwatch()
        from tkinter import TclError
        try:
            app.destroy()
        except TclError:
            pass
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tokenote Agent token usage meter")
    parser.add_argument("--sync-only", action="store_true", help="只同步一次并打印结果")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite 数据库路径")
    parser.add_argument("--version", action="version", version=f"Tokenote {VERSION}")
    args = parser.parse_args()
    # Acquire before initializing SQLite or starting any adapter. Windows kernel
    # ownership also covers a concurrent --sync-only process using the same DB.
    guard = SingleInstance(args.db) if sys.platform == "win32" else nullcontext(None)
    with guard as instance:
        if instance is not None and not instance.acquired:
            if args.sync_only:
                print("Tokenote 正在使用这份统计库，已跳过本次同步。")
            else:
                instance.request_activation()
            return 0
        ensure_data_dir()
        return run_sync(args.db) if args.sync_only else run_gui(args.db, instance)


if __name__ == "__main__":
    sys.exit(main())
