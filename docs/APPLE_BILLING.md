# Apple Billing (StoreKit 2 + App Store Server Notifications v2)

This document describes the Firestore layout and state transitions used by the
server endpoints in `app/routes/billing.py`.

## Firestore schema

### `users/{uid}/subscriptions/apple`
Latest Apple subscription snapshot for the user.

- `transactionId` (string)
- `originalTransactionId` (string)
- `productId` (string)
- `appAccountToken` (string)
- `environment` (string)
- `purchaseDateMs` (int)
- `purchaseAt` (timestamp)
- `expiresDateMs` (int)
- `expiresAt` (timestamp)
- `revocationDateMs` (int)
- `revocationAt` (timestamp)
- `status` (string: `active`, `grace_period`, `billing_retry`, `expired`, `revoked`)
- `plan` (string: `free`, `basic`, `pro`)
- `renewalInfo` (map, optional)
- `lastNotificationType` (string, optional)
- `lastNotificationSubtype` (string, optional)
- `lastNotificationUUID` (string, optional)
- `source` (string: `app_confirm` or `app_store_notification`)
- `updatedAt` (timestamp)

### `apple_transactions/{originalTransactionId}`
Global subscription index keyed by the original transaction id.

- Same fields as `users/{uid}/subscriptions/apple`
- `uid` (string, optional)
- `lastEventAt` (timestamp)

### `apple_app_account_tokens/{appAccountToken}`
Mapping between appAccountToken and a user.

- `uid` (string)
- `originalTransactionId` (string, optional)
- `createdAt` (timestamp)
- `updatedAt` (timestamp)

### `apple_notifications/{notificationUUID}`
Minimal audit log for processed notifications.

- `notificationType` (string)
- `subtype` (string, optional)
- `environment` (string, optional)
- `originalTransactionId` (string)
- `transactionId` (string)
- `uid` (string, optional)
- `receivedAt` (timestamp)

## Plan mapping

The product id is mapped to a plan using simple string matching:

- `pro` or `premium` -> `pro`
- `basic` or `standard` -> `basic`
- default -> `pro`

## Entitlement decision

Entitlement is granted when:

- status is not `revoked` or `expired`, and
- `expiresDateMs` (if present) is in the future.

If entitlement is not granted, the user plan is set to `free`.

## Notification status transitions

The server derives a `status` from notification type + transaction/renewal data:

| Notification type              | Status         | Notes |
|-------------------------------|----------------|-------|
| `DID_REVOKE`, `REFUND`        | `revoked`      | Immediate revoke |
| `EXPIRED`, `GRACE_PERIOD_EXPIRED` | `expired`  | Subscription ended |
| `DID_FAIL_TO_RENEW`           | `billing_retry`| Access continues until expiry |
| Other types                   | `active`       | Default state |

Additional overrides:

- `revocationDate` => `revoked`
- `expiresDate` in the past => `expired`
- `gracePeriodExpiresDate` in the future => `grace_period`
