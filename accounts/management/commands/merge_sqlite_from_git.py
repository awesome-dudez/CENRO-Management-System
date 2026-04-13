"""
Merge rows from db.sqlite3 as stored in Git into the local SQLite database.

Uses INSERT OR IGNORE: primary keys already present in local are kept (local wins);
rows that exist only in the Git copy are copied in.

Skips django_migrations (keep local migration history).

Usage:
  python manage.py merge_sqlite_from_git
  python manage.py merge_sqlite_from_git --ref origin/main
  python manage.py merge_sqlite_from_git --dry-run
"""
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


SKIP_TABLES = frozenset({"django_migrations"})


class Command(BaseCommand):
    help = "Merge data from Git-tracked db.sqlite3 into the local SQLite database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ref",
            default="origin/main",
            help="Git ref containing db.sqlite3 (default: origin/main).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be merged without writing to the local database.",
        )

    def handle(self, *args, **options):
        ref = options["ref"]
        dry_run = options["dry_run"]

        db = settings.DATABASES["default"]
        if db.get("ENGINE") != "django.db.backends.sqlite3":
            raise CommandError("This command only works with SQLite (local ENGINE).")

        db_path = Path(db["NAME"])
        repo_root = Path(settings.BASE_DIR)

        if not db_path.is_file():
            raise CommandError(f"Local database not found: {db_path}")

        try:
            blob = subprocess.run(
                ["git", "show", f"{ref}:db.sqlite3"],
                cwd=repo_root,
                capture_output=True,
                check=True,
            ).stdout
        except subprocess.CalledProcessError as e:
            raise CommandError(
                f"Could not read db.sqlite3 from {ref}. "
                f"Fetch the ref (git fetch) and ensure the file exists in that commit.\n"
                f"{e.stderr.decode(errors='replace') if e.stderr else e}"
            ) from e

        if not blob:
            raise CommandError(
                f"Empty db.sqlite3 at {ref}:db.sqlite3 (0 bytes). "
                f"Use an older ref that contains data, e.g. "
                f"`python manage.py merge_sqlite_from_git --ref c274fe3`, "
                f"or commit a non-empty database to the repo."
            )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: no changes will be made."))

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tmp:
            tmp.write(blob)
            repo_db_path = Path(tmp.name)

        try:
            self._merge(repo_db_path, db_path, dry_run=dry_run)
        finally:
            try:
                repo_db_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "Merge finished. Run `python manage.py migrate` if the repo DB was behind on migrations."
                )
            )

    def _merge(self, repo_db_path: Path, local_db_path: Path, *, dry_run: bool):
        if not dry_run:
            bak = local_db_path.with_suffix(
                f".sqlite3.bak.{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            shutil.copy2(local_db_path, bak)
            self.stdout.write(self.style.NOTICE(f"Backed up local DB to {bak}"))

        local = sqlite3.connect(str(local_db_path))
        try:
            local.execute("PRAGMA foreign_keys=OFF")
            local.execute("ATTACH DATABASE ? AS repo", (str(repo_db_path),))

            tables = local.execute(
                """
                SELECT name FROM repo.sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            merged = 0
            skipped = 0

            for (tbl,) in tables:
                if tbl in SKIP_TABLES:
                    skipped += 1
                    continue
                exists = local.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,),
                ).fetchone()
                if not exists:
                    self.stdout.write(
                        self.style.WARNING(f"Skip {tbl}: not present in local database")
                    )
                    skipped += 1
                    continue

                main_cols = [
                    r[1]
                    for r in local.execute(f'PRAGMA main.table_info("{tbl}")').fetchall()
                ]
                repo_cols = {
                    r[1]
                    for r in local.execute(f'PRAGMA repo.table_info("{tbl}")').fetchall()
                }
                common = [c for c in main_cols if c in repo_cols]
                if not common:
                    skipped += 1
                    continue

                quoted = ", ".join(f'"{c}"' for c in common)
                sql = (
                    f'INSERT OR IGNORE INTO main."{tbl}" ({quoted}) '
                    f'SELECT {quoted} FROM repo."{tbl}"'
                )
                if dry_run:
                    n_repo = local.execute(f'SELECT COUNT(*) FROM repo."{tbl}"').fetchone()[0]
                    self.stdout.write(f"Would merge table {tbl} ({n_repo} rows in Git copy, {len(common)} columns)")
                    merged += 1
                else:
                    cur = local.execute(sql)
                    merged += 1
                    if cur.rowcount and cur.rowcount > 0:
                        self.stdout.write(f"  {tbl}: inserted up to {cur.rowcount} new rows (IGNORE duplicates)")

            if not dry_run:
                local.commit()
            try:
                local.execute("DETACH DATABASE repo")
            except sqlite3.OperationalError as e:
                self.stdout.write(self.style.WARNING(f"DETACH repo: {e} (data was committed)"))
            self.stdout.write(
                f"Tables processed: {merged}, skipped: {skipped} (incl. django_migrations & missing tables)."
            )
        finally:
            try:
                local.execute("PRAGMA foreign_keys=ON")
            except sqlite3.Error:
                pass
            local.close()
