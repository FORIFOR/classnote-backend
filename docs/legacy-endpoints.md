# Legacy / Sunsetted Endpoints

When a production endpoint is intentionally retired, it MUST appear in
this file with its sunset date BEFORE it can be removed from the build.

`tools/route_inventory.py diff` reads this file as the allowlist for
"removed routes are OK because they were sunsetted". Any production route
removed without an entry here will block deploy.

## Format

One bullet per `METHOD path` plus a sunset date in YYYY-MM-DD:

```
- GET /old/path  sunset:2026-01-31  reason:replaced by /v2/new/path
```

## Lifecycle

```
stable → legacy → deprecated → removed
```

Do not jump stages. Each stage requires:
- `stable → legacy`: announcement + deprecation header in response
- `legacy → deprecated`: at least 30 days of `Deprecation:` headers + monitoring of clients still calling
- `deprecated → removed`: 0 calls in last 7 days + entry in this file

## Active sunsets

(none yet)
