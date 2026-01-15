
import logging
from datetime import datetime, timedelta
from typing import Optional
from google.cloud import firestore
from app.firebase import db

logger = logging.getLogger("app.idempotency")

class ResourceAlreadyProcessed(Exception):
    pass

class IdempotencyManager:
    """
    Firestore-based idempotency lock manager.
    Prevents duplicate processing of background tasks.
    """
    def __init__(self, collection_name: str = "idempotency_locks"):
        self.collection = db.collection(collection_name)

    async def check_and_lock(self, 
                             key: str, 
                             context: str, 
                             ttl_seconds: int = 3600) -> bool:
        """
        Attempts to acquire a lock for the given key.
        Returns True if lock acquired (first time), Raises Exception if already processed/processing.
        """
        doc_ref = self.collection.document(key)
        
        # In a real app, use transaction. 
        # For this pattern demonstration, we rely on create/get logic.
        try:
            doc = doc_ref.get()
            if doc.exists:
                data = doc.to_dict()
                created_at = data.get("createdAt")
                
                # Check consistency
                if data.get("status") == "completed":
                    raise ResourceAlreadyProcessed(f"Key {key} already completed.")
                
                # Check TTL (if stuck in processing)
                if created_at:
                    now = datetime.utcnow()
                    # simplistic check
                    # if now - created_at > timedelta(seconds=ttl_seconds):
                    #    # Expired, might allow retry?
                    #    pass
                
                raise ResourceAlreadyProcessed(f"Key {key} is processing or completed.")
            
            # Create lock
            doc_ref.set({
                "context": context,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "status": "processing"
            })
            return True
            
        except ResourceAlreadyProcessed:
            raise
        except Exception as e:
            logger.error(f"Idempotency check failed: {e}")
            # Fail safe: allow processing or deny? Deny is safer.
            raise e

    async def mark_completed(self, key: str, result: Optional[dict] = None):
        """
        Marks the task as successfully completed.
        """
        self.collection.document(key).update({
            "status": "completed",
            "completedAt": firestore.SERVER_TIMESTAMP,
            "result": result
        })

    async def mark_failed(self, key: str, error: str):
        """
        Marks the task as failed (allows release of lock if needed, or permanent fail).
        """
        self.collection.document(key).update({
            "status": "failed",
            "error": error
        })

# Global instance
idempotency = IdempotencyManager()
