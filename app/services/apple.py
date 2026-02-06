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
        # Dual-environment verifiers for handling both Production and Sandbox
        self.verifier_production = None
        self.verifier_sandbox = None

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

                # Create verifiers for both environments to handle Production and Sandbox JWS tokens
                print(f"[Apple] Creating dual SignedDataVerifiers with app_apple_id={self.app_apple_id}...")
                self.verifier_production = SignedDataVerifier(
                    root_certificates,
                    self.enable_online_checks,
                    VerifierEnvironment.PRODUCTION,
                    self.bundle_id,
                    self.app_apple_id
                )
                self.verifier_sandbox = SignedDataVerifier(
                    root_certificates,
                    self.enable_online_checks,
                    VerifierEnvironment.SANDBOX,
                    self.bundle_id,
                    self.app_apple_id
                )
                # Set primary verifier based on configured environment
                self.verifier = self.verifier_production if self.environment == VerifierEnvironment.PRODUCTION else self.verifier_sandbox
                print(f"[Apple] AppStoreService initialized with dual verifiers (primary={self.environment.name}, online_checks={self.enable_online_checks}).")
            except Exception as e:
                import traceback
                print(f"[Apple] ERROR: Failed to initialize AppStoreService: {e}")
                print(traceback.format_exc())
        else:
            logger.warning("AppStoreService not initialized. Missing environment variables.")

    def _decode_transaction(self, verifier: SignedDataVerifier, signed_payload: str) -> Dict[str, Any]:
        """Helper to decode a signed transaction and extract fields."""
        decoded = verifier.verify_and_decode_signed_transaction(signed_payload)
        return {
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

    def verify_jws_detailed(self, signed_payload: str) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
        if not self.verifier:
            return None, {
                "stage": "init",
                "error_type": "verifier_not_initialized",
                "error_message": "AppStoreService verifier not initialized.",
            }

        # Try with primary verifier first
        try:
            result = self._decode_transaction(self.verifier, signed_payload)
            print(f"[Apple] Decoded (primary): otid={result.get('originalTransactionId')}, productId={result.get('productId')}, env={result.get('environment')}")
            return result, None
        except Exception as primary_error:
            primary_error_msg = str(primary_error)
            # Check if this is an environment mismatch error
            if "INVALID_ENVIRONMENT" in primary_error_msg:
                # Try the alternate verifier
                alternate_verifier = self.verifier_sandbox if self.verifier == self.verifier_production else self.verifier_production
                if alternate_verifier:
                    try:
                        result = self._decode_transaction(alternate_verifier, signed_payload)
                        alt_env = "sandbox" if alternate_verifier == self.verifier_sandbox else "production"
                        print(f"[Apple] Decoded (fallback {alt_env}): otid={result.get('originalTransactionId')}, productId={result.get('productId')}, env={result.get('environment')}")
                        return result, None
                    except Exception as fallback_error:
                        # Both verifiers failed
                        return None, {
                            "stage": "verify",
                            "error_type": type(fallback_error).__name__,
                            "error_message": f"Both verifiers failed. Primary: {primary_error_msg}, Fallback: {str(fallback_error)}",
                        }
            # Not an environment error, or no alternate verifier available
            return None, {
                "stage": "verify",
                "error_type": type(primary_error).__name__,
                "error_message": primary_error_msg,
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
        Tries both Production and Sandbox verifiers if needed.
        """
        if not self.verifier:
            logger.error("AppStoreService verifier not initialized.")
            return None

        # Try primary verifier first
        try:
            return self.verifier.verify_and_decode_notification(signed_payload)
        except Exception as primary_error:
            primary_error_msg = str(primary_error)
            if "INVALID_ENVIRONMENT" in primary_error_msg:
                # Try alternate verifier
                alternate_verifier = self.verifier_sandbox if self.verifier == self.verifier_production else self.verifier_production
                if alternate_verifier:
                    try:
                        result = alternate_verifier.verify_and_decode_notification(signed_payload)
                        alt_env = "sandbox" if alternate_verifier == self.verifier_sandbox else "production"
                        logger.info(f"Notification verified with fallback verifier ({alt_env})")
                        return result
                    except Exception as fallback_error:
                        logger.error(f"Notification verification failed with both verifiers. Primary: {primary_error_msg}, Fallback: {fallback_error}")
                        return None
            logger.error(f"Notification verification failed: {primary_error}")
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
         # Use get_all_subscription_statuses for the most accurate data
         statuses = self.get_all_subscription_statuses(original_transaction_id)
         if statuses and statuses.get("data"):
             for sub_group in statuses["data"]:
                 last_txns = sub_group.get("lastTransactions", [])
                 if last_txns:
                     # Return the most recent transaction
                     signed_txn = last_txns[0].get("signedTransactionInfo")
                     if signed_txn:
                         return self.verify_jws(signed_txn)
         return None

    def get_all_subscription_statuses(self, original_transaction_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch all subscription statuses for a given originalTransactionId.

        This is the Apple-recommended way to:
        - Verify subscription ownership during account linking
        - Reconcile subscription state after missed notifications
        - Get the complete subscription history

        Returns:
            dict with structure:
            {
                "environment": "Production" | "Sandbox",
                "bundleId": "...",
                "appAppleId": 123,
                "data": [
                    {
                        "subscriptionGroupIdentifier": "...",
                        "lastTransactions": [
                            {
                                "originalTransactionId": "...",
                                "status": 1,  # 1=Active, 2=Expired, 3=Billing Retry, 4=Grace Period, 5=Revoked
                                "signedTransactionInfo": "...",
                                "signedRenewalInfo": "..."
                            }
                        ]
                    }
                ]
            }
        """
        if not self.client:
            logger.error("AppStoreService client not initialized.")
            return None

        try:
            # API Call: Get All Subscription Statuses
            # https://developer.apple.com/documentation/appstoreserverapi/get_all_subscription_statuses
            response = self.client.get_all_subscription_statuses(
                original_transaction_id,
                status=None  # Get all statuses (active, expired, etc.)
            )

            # Convert response to dict
            result = {
                "environment": str(response.environment) if response.environment else None,
                "bundleId": response.bundleId,
                "appAppleId": response.appAppleId,
                "data": []
            }

            # Process subscription group data
            if response.data:
                for sub_group in response.data:
                    group_data = {
                        "subscriptionGroupIdentifier": sub_group.subscriptionGroupIdentifier,
                        "lastTransactions": []
                    }

                    if sub_group.lastTransactions:
                        for txn in sub_group.lastTransactions:
                            txn_data = {
                                "originalTransactionId": txn.originalTransactionId,
                                "status": txn.status,  # SubscriptionStatus enum
                                "signedTransactionInfo": txn.signedTransactionInfo,
                                "signedRenewalInfo": txn.signedRenewalInfo
                            }
                            group_data["lastTransactions"].append(txn_data)

                    result["data"].append(group_data)

            logger.info(f"[Apple] Got subscription statuses for {original_transaction_id}: {len(result['data'])} groups")
            return result

        except Exception as e:
            error_str = str(e)
            # Handle specific error codes
            if "4040010" in error_str:  # ORIGINAL_TRANSACTION_ID_NOT_FOUND
                logger.warning(f"[Apple] originalTransactionId not found: {original_transaction_id}")
                return None
            logger.error(f"[Apple] Get All Subscription Statuses failed: {e}")
            return None

    def get_subscription_status_for_account(
        self,
        original_transaction_id: str,
        expected_app_account_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get subscription status and verify ownership.

        Args:
            original_transaction_id: The subscription's originalTransactionId
            expected_app_account_token: If provided, verify this token matches

        Returns:
            dict with:
            {
                "found": bool,
                "active": bool,
                "status": "active" | "expired" | "billing_retry" | "grace_period" | "revoked",
                "expires_date": int (ms),
                "product_id": str,
                "app_account_token": str | None,
                "token_matches": bool | None,
                "transaction": dict | None,
                "renewal_info": dict | None
            }
        """
        result = {
            "found": False,
            "active": False,
            "status": "unknown",
            "expires_date": None,
            "product_id": None,
            "app_account_token": None,
            "token_matches": None,
            "transaction": None,
            "renewal_info": None
        }

        statuses = self.get_all_subscription_statuses(original_transaction_id)
        if not statuses or not statuses.get("data"):
            return result

        # Find the matching transaction
        for sub_group in statuses["data"]:
            for txn in sub_group.get("lastTransactions", []):
                if txn.get("originalTransactionId") == original_transaction_id:
                    result["found"] = True

                    # Decode transaction info
                    if txn.get("signedTransactionInfo"):
                        transaction = self.verify_jws(txn["signedTransactionInfo"])
                        if transaction:
                            result["transaction"] = transaction
                            result["expires_date"] = transaction.get("expiresDate")
                            result["product_id"] = transaction.get("productId")
                            result["app_account_token"] = transaction.get("appAccountToken")

                            # Check token match
                            if expected_app_account_token:
                                result["token_matches"] = (
                                    transaction.get("appAccountToken") == expected_app_account_token
                                )

                    # Decode renewal info
                    if txn.get("signedRenewalInfo"):
                        renewal = self.verify_renewal_info(txn["signedRenewalInfo"])
                        if renewal:
                            result["renewal_info"] = {
                                "autoRenewStatus": getattr(renewal, "autoRenewStatus", None),
                                "autoRenewProductId": getattr(renewal, "autoRenewProductId", None),
                                "expirationIntent": getattr(renewal, "expirationIntent", None),
                                "gracePeriodExpiresDate": getattr(renewal, "gracePeriodExpiresDate", None),
                                "isInBillingRetryPeriod": getattr(renewal, "isInBillingRetryPeriod", None),
                            }

                    # Map status code to string
                    # 1=Active, 2=Expired, 3=Billing Retry, 4=Grace Period, 5=Revoked
                    status_code = txn.get("status")
                    status_map = {
                        1: ("active", True),
                        2: ("expired", False),
                        3: ("billing_retry", True),  # Still has access during retry
                        4: ("grace_period", True),   # Still has access during grace
                        5: ("revoked", False),
                    }
                    if status_code in status_map:
                        result["status"], result["active"] = status_map[status_code]

                    return result

        return result


def set_app_account_token(
        self,
        original_transaction_id: str,
        app_account_token: str
    ) -> bool:
        """
        Set the appAccountToken for a subscription.

        This is used to link a subscription to your user account when:
        - User redeemed an Offer Code outside the app (App Store / URL)
        - Need to fix/update the account linkage

        Args:
            original_transaction_id: The subscription's originalTransactionId
            app_account_token: Your user ID as UUID format

        Returns:
            True if successful, False otherwise

        Reference:
            https://developer.apple.com/documentation/appstoreserverapi/set_app_account_token
        """
        if not self.client:
            logger.error("AppStoreService client not initialized.")
            return False

        try:
            # The appstoreserverlibrary should have this method
            # API: PUT /inApps/v1/transactions/{originalTransactionId}/appAccountToken/{appAccountToken}
            from appstoreserverlibrary.models.SetAppAccountTokenRequest import SetAppAccountTokenRequest

            request = SetAppAccountTokenRequest(appAccountToken=app_account_token)
            self.client.send_app_account_token(original_transaction_id, request)

            logger.info(f"[Apple] Set appAccountToken for {original_transaction_id}: {app_account_token}")
            return True

        except ImportError:
            # Fallback: Manual API call if library doesn't have the method
            logger.warning("[Apple] SetAppAccountTokenRequest not available in library, using manual call")
            try:
                import jwt
                import time
                import requests

                # Build JWT for API auth
                now = int(time.time())
                payload = {
                    "iss": self.issuer_id,
                    "iat": now,
                    "exp": now + 3600,
                    "aud": "appstoreconnect-v1",
                    "bid": self.bundle_id,
                }

                formatted_key = self.private_key.replace("\\n", "\n")
                if "-----BEGIN PRIVATE KEY-----" not in formatted_key:
                    formatted_key = f"-----BEGIN PRIVATE KEY-----\n{formatted_key}\n-----END PRIVATE KEY-----"

                token = jwt.encode(
                    payload,
                    formatted_key,
                    algorithm="ES256",
                    headers={"kid": self.key_id}
                )

                # Determine base URL
                if self.client_environment.name == "PRODUCTION":
                    base_url = "https://api.storekit.itunes.apple.com"
                else:
                    base_url = "https://api.storekit-sandbox.itunes.apple.com"

                url = f"{base_url}/inApps/v1/transactions/{original_transaction_id}/appAccountToken/{app_account_token}"

                response = requests.put(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30
                )

                if response.status_code == 200:
                    logger.info(f"[Apple] Set appAccountToken for {original_transaction_id}: {app_account_token}")
                    return True
                else:
                    logger.error(f"[Apple] Set appAccountToken failed: {response.status_code} {response.text}")
                    return False

            except Exception as e:
                logger.error(f"[Apple] Set appAccountToken manual call failed: {e}")
                return False

        except Exception as e:
            logger.error(f"[Apple] Set appAccountToken failed: {e}")
            return False


# Singleton
apple_service = AppStoreService()
