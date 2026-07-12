import os
import shutil
import glob
import logging
from datetime import datetime
import db_utils

logger = logging.getLogger(__name__)

class DBManager:
    def __init__(self):
        self.project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.db_path = os.path.join(self.project_dir, "quant_system.db")
        self.backup_dir = os.path.join(self.project_dir, "backups")
        os.makedirs(self.backup_dir, exist_ok=True)
        self.max_backups = 30 # Keep last 30 backups

    def backup(self, prefix="quant_system"):
        """Creates a snapshot backup of the SQLite database and rotates old backups."""
        if not os.path.exists(self.db_path):
            logger.warning(f"Database {self.db_path} does not exist. Skipping backup.")
            return None
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(self.backup_dir, f"{prefix}_{timestamp}.db")
        
        try:
            # Safe copy
            shutil.copy2(self.db_path, backup_path)
            logger.info(f"Successfully backed up DB to {backup_path}")
            
            # Prune old backups
            self._prune_backups(prefix)
            return backup_path
        except Exception as e:
            logger.error(f"Failed to backup DB: {e}")
            raise e
            
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
