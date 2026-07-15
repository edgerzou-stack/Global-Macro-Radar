import os
import glob
import logging
import sqlite3
import tempfile
from datetime import datetime
import db_utils

logger = logging.getLogger(__name__)

class DBManager:
    def __init__(self, db_path=None, backup_dir=None, max_backups=30):
        self.db_path = db_utils.normalize_db_path(db_path or db_utils.get_db_path())
        self.project_dir = os.path.dirname(self.db_path)
        self.backup_dir = os.path.realpath(
            os.path.abspath(backup_dir or os.path.join(self.project_dir, "backups"))
        )
        os.makedirs(self.backup_dir, exist_ok=True)
        self.max_backups = max_backups

    def backup(self, prefix="quant_system"):
        """Creates a snapshot backup of the SQLite database and rotates old backups."""
        if not os.path.exists(self.db_path):
            logger.warning(f"Database {self.db_path} does not exist. Skipping backup.")
            return None
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = os.path.join(self.backup_dir, f"{prefix}_{timestamp}.db")
        temp_path = None

        try:
            temp_file = tempfile.NamedTemporaryFile(
                prefix=f".{prefix}_",
                suffix=".tmp",
                dir=self.backup_dir,
                delete=False,
            )
            temp_path = temp_file.name
            temp_file.close()

            # SQLite's online backup API produces a transactionally consistent
            # snapshot and includes committed pages that are still in the WAL.
            with sqlite3.connect(self.db_path, timeout=30.0) as source:
                with sqlite3.connect(temp_path, timeout=30.0) as destination:
                    source.backup(destination)

            with sqlite3.connect(temp_path, timeout=30.0) as verification:
                integrity = verification.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    raise sqlite3.DatabaseError(
                        f"Backup integrity check failed: {integrity!r}"
                    )

            os.replace(temp_path, backup_path)
            temp_path = None
            logger.info(f"Successfully backed up DB to {backup_path}")
            
            # Prune old backups
            self._prune_backups(prefix)
            return backup_path
        except Exception as e:
            if temp_path and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    logger.warning("Failed to remove incomplete backup %s", temp_path)
            logger.error(f"Failed to backup DB: {e}")
            raise
            
    def _prune_backups(self, prefix):
        """Removes oldest backups exceeding max_backups limit."""
        pattern = os.path.join(self.backup_dir, f"{prefix}_*.db")
        backups = glob.glob(pattern)
        # Sort by modification time (oldest first)
        backups.sort(key=os.path.getmtime)
        
        while len(backups) > self.max_backups:
            oldest = backups.pop(0)
            try:
                os.remove(oldest)
                logger.info(f"Pruned old backup: {oldest}")
            except Exception as e:
                logger.warning(f"Failed to prune old backup {oldest}: {e}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    manager = DBManager()
    manager.backup(prefix="manual_snapshot")
