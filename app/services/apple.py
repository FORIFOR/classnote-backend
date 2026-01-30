import os
import logging
from typing import Optional, Dict, Any, List

from appstoreserverlibrary.api_client import AppStoreServerAPIClient, Environment as ClientEnvironment
from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
from appstoreserverlibrary.models.Environment import Environment as VerifierEnvironment

logger = logging.getLogger("app.services.apple")


def _load_root_certificates(cert_dir: str) -> List[bytes]:
    certs: List[bytes] = []
    if not os.path.isdir(cert_dir):
        logger.error("Apple root cert directory missing: %s", cert_dir)
        return certs
    for name in sorted(os.listdir(cert_dir)):
        if not name.lower().endswith(".cer"):
            continue
        path = os.path.join(cert_dir, name)
        try:
            with open(path, "rb") as cert_file:
                certs.append(cert_file.read())
        except OSError as exc:
            logger.error("Failed to read Apple root cert %s: %s", path, exc)
    if not certs:
        logger.error("No Apple root certificates loaded from %s", cert_dir)
    return certs

class AppStoreService:
    def __init__(self):
        self.issuer_id = os.getenv("APPLE_ISSUER_ID")
        self.key_id = os.getenv("APPLE_KEY_ID")
        self.private_key = os.getenv("APPLE_KEY_P8")
        self.bundle_id = os.getenv("APPLE_BUNDLE_ID")
        # app_apple_id must be an int for SignedDataVerifier
        app_apple_id_str = os.getenv("APPLE_APPLE_ID")
        self.app_apple_id = int(app_apple_id_str) if app_apple_id_str else None
        self.environment = VerifierEnvironment.SANDBOX  # Default to Sandbox, override checking logic later or via Env
        self.client_environment = ClientEnvironment.SANDBOX
        self.enable_online_checks = True
        
        # Check environment override
        env_str = os.getenv("APPLE_ENVIRONMENT", "Sandbox")
        if env_str.lower() == "production":
            self.environment = VerifierEnvironment.PRODUCTION
            self.client_environment = ClientEnvironment.PRODUCTION

        self.client = None
        self.verifier = None

        if self.issuer_id and self.key_id and self.private_key and self.bundle_id:
            try:
                # Basic cleanup of private key if it comes as a single line string
                formatted_key = self.private_key.replace("\\n", "\n")
                if "-----BEGIN PRIVATE KEY-----" not in formatted_key:
                     formatted_key = f"-----BEGIN PRIVATE KEY-----\n{formatted_key}\n-----END PRIVATE KEY-----"
                # Encode to bytes for cryptography library
                formatted_key_bytes = formatted_key.encode("utf-8")

                print("[Apple] Creating AppStoreServerAPIClient...")
                self.client = AppStoreServerAPIClient(
                    formatted_key_bytes,
                    self.key_id,
                    self.issuer_id,
                    self.bundle_id,
                    self.client_environment
                )
                print("[Apple] AppStoreServerAPIClient created successfully.")

                cert_dir = os.getenv(
                    "APPLE_ROOT_CERT_DIR",
                    os.path.normpath(
                        os.path.join(os.path.dirname(__file__), "..", "certs", "apple")
                    ),
                )
                print(f"[Apple] Loading root certificates from: {cert_dir}")
                root_certificates = _load_root_certificates(cert_dir)
                print(f"[Apple] Loaded {len(root_certificates)} root certificates (sizes: {[len(c) for c in root_certificates]})")

                online_checks_env = os.getenv("APPLE_ENABLE_ONLINE_CHECKS", "true")
                self.enable_online_checks = online_checks_env.lower() in {"1", "true", "yes"}

                print(f"[Apple] Creating SignedDataVerifier with app_apple_id={self.app_apple_id} (type={type(self.app_apple_id).__name__})...")
                self.verifier = SignedDataVerifier(
                    root_certificates,
                    self.enable_online_checks,
                    self.environment,
                    self.bundle_id,
                    self.app_apple_id
                )
                print(f"[Apple] AppStoreService initialized in {self.environment.name} mode (online_checks={self.enable_online_checks}).")
            except Exception as e:
                import traceback
                print(f"[Apple] ERROR: Failed to initialize AppStoreService: {e}")
                print(traceback.format_exc())
        else:
            logger.warning("AppStoreService not initialized. Missing environment variables.")

    def verify_jws_detailed(self, signed_payload: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
        if not self.verifier:
            return None, {
                "stage": "init",
                "error_type": "verifier_not_initialized",
                "error_message": "AppStoreService verifier not initialized.",
            }
        try:
            decoded = self.verifier.verify_and_decode_signed_transaction(signed_payload)
            # Extract all known fields using getattr (slots-compatible)
            result = {
                'originalTransactionId': getattr(decoded, 'originalTransactionId', None),
                'transactionId': getattr(decoded, 'transactionId', None),
                'productId': getattr(decoded, 'productId', None),
                'bundleId': getattr(decoded, 'bundleId', None),
                'environment': getattr(decoded, 'environment', None),
                'expiresDate': getattr(decoded, 'expiresDate', None),
                'appAccountToken': getattr(decoded, 'appAccountToken', None),
                'originalPurchaseDate': getattr(decoded, 'originalPurchaseDate', None),
                'purchaseDate': getattr(decoded, 'purchaseDate', None),
                'signedDate': getattr(decoded, 'signedDate', None),
                'inAppOwnershipType': getattr(decoded, 'inAppOwnershipType', None),
                'subscriptionGroupIdentifier': getattr(decoded, 'subscriptionGroupIdentifier', None),
                'isUpgraded': getattr(decoded, 'isUpgraded', None),
                'revocationDate': getattr(decoded, 'revocationDate', None),
                'revocationReason': getattr(decoded, 'revocationReason', None),
                'offerType': getattr(decoded, 'offerType', None),
                'offerIdentifier': getattr(decoded, 'offerIdentifier', None),
            }
            print(f"[Apple] Decoded: otid={result.get('originalTransactionId')}, productId={result.get('productId')}")
            return result, None
        except Exception as e:
            return None, {
                "stage": "verify",
                "error_type": type(e).__name__,
                "error_message": str(e),
            }

    def verify_jws(self, signed_payload: str) -> Optional[Dict[str, Any]]:
        """
        Verify a JWS (transaction or response) completely.
        Returns the decoded payload if valid, None otherwise.
        """
        decoded, error = self.verify_jws_detailed(signed_payload)
        if error:
            logger.error("JWS Verification failed: %s", error.get("error_message"))
        return decoded

    def verify_notification(self, signed_payload: str) -> Optional[Any]:
        """
        Verify a signed App Store Server Notification payload.
        Returns the decoded notification object if valid, None otherwise.
        """
        if not self.verifier:
            logger.error("AppStoreService verifier not initialized.")
            return None

        try:
            return self.verifier.verify_and_decode_notification(signed_payload)
        except Exception as e:
            logger.error(f"Notification verification failed: {e}")
            return None

    def verify_renewal_info(self, signed_payload: str) -> Optional[Any]:
        """
        Verify a signed renewal info JWS from a notification.
        Returns the decoded renewal info if valid, None otherwise.
        """
        if not self.verifier:
            logger.error("AppStoreService verifier not initialized.")
            return None

        try:
            if hasattr(self.verifier, "verify_and_decode_renewal_info"):
                return self.verifier.verify_and_decode_renewal_info(signed_payload)
            logger.warning("SignedDataVerifier lacks verify_and_decode_renewal_info; skipping decode.")
            return None
        except Exception as e:
            logger.error(f"Renewal info verification failed: {e}")
            return None

    def get_transaction_info(self, transaction_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch latest transaction info from Apple API using transaction_id.
        Useful to check latest status/revocation.
        """
        if not self.client:
            logger.error("AppStoreService client not initialized.")
            return None
            
        try:
            # API Call: Get Transaction Info
            response = self.client.get_transaction_info(transaction_id)
            if response.signedTransactionInfo:
                # We should verify this JWS too!
                return self.verify_jws(response.signedTransactionInfo)
            return None
        except Exception as e:
            logger.error(f"Get Transaction Info failed: {e}")
            return None
            
    def get_last_transaction(self, original_transaction_id: str) -> Optional[Dict[str, Any]]:
         """
         Fetch subscription status/history to get the very latest transaction.
         """
         # Simplified for MVP: just use get_transaction_info if we have a recent transaction_id
         # For robust logic, we might need get_all_subscription_statuses
         return self.get_transaction_info(original_transaction_id)


# Singleton
apple_service = AppStoreService()
