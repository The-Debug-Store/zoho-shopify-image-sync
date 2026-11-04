# Zoho–Shopify item image sync: dependencies (DEP-1)

Last updated: 04/11/2026 (draft, not yet deployed)

## Purpose
Monthly job: for each active Zoho Books item (Debug Store, org 61706903) with no image,
copy the featured image of the Shopify product with the same SKU onto the Zoho item.
Items with no SKU match or no Shopify image are reported, not failed silently.

## Status
- Script `sync_images.py`: written, compiles. Dry run not yet executed live.
- Zoho image upload: DISABLED (`UPLOAD_ENDPOINT_VERIFIED = False`). Endpoint not in the
  Zoho Books connector and not confirmed in official docs. Candidate from a forum post:
  `POST https://www.zohoapis.com/books/v3/items/{item_id}/image?organization_id=...`,
  multipart field `image`. Forum poster reported error 33003. Needs one live test on one item.
- GitHub repo: not created yet (John to create).
- Scheduler: not set up yet (planned GitHub Actions, monthly cron).

## Apps and connections
| Name | System | Purpose | Owner | Where credentials are held |
|---|---|---|---|---|
| Zoho Self Client | Zoho API console (accounts.zoho.com, US DC) | Read items, write item images | John | GitHub Secrets: ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID |
| Shopify custom app | Shopify admin, Debug Store | Read products and variants (read_products) | John | GitHub Secrets: SHOPIFY_SHOP_DOMAIN, SHOPIFY_ADMIN_TOKEN |
| GitHub repo `zoho-shopify-image-sync` | GitHub | Holds script, schedule and reports | John | GitHub account; Actions secrets |

Not used: the Claude Zoho Books connector (no image write), Zoho Flow, n8n.

## Scopes
- Zoho Books: items fall under the settings family (Zoho docs table: "settings: Access the APIs related to Items, ...").
  Needed: `ZohoBooks.settings.READ` (list and read items) and `ZohoBooks.settings.UPDATE` (write item image).
  Do not request `ZohoBooks.settings.ALL` (includes CREATE and DELETE). Verified against Zoho's OAuth scopes table
  (zoho.com/books/api/v3/oauth/#scopes), fetched 04/11/2026.
- Shopify: `read_products` (variant SKU, product) and `read_files` (MediaImage featured image). Both are needed: the script's
  featured-image query returns MediaImage, which Shopify lists under read_files, not read_products. No write scopes.
  Source: shopify.dev access-scopes table, checked 04/11/2026.

## Webhooks
None. Batch job only.

## Schedule
Monthly, 1st of each month 06:00 Europe/London in summer (05:00 UTC cron; GMT is 1 hour later). [INFERRED]

## Outputs
- `reports/image-sync-<run_id>.csv` per row: item, SKU, status, detail
- `reports/image-sync-<run_id>.json` run totals: matched, uploaded, skipped, failed
- Exit code 1 if any failure (alerts via failed workflow run).

## Data touched
- Zoho: GET items (paged), GET item (read-back), POST item image (when enabled).
- Shopify: GraphQL productVariants query by SKU, image URL download from Shopify CDN.

## What breaks if removed
- Zoho items stay without images; Harry would upload by hand.
- No effect on Shopify (read-only).

## Known gaps and risks
- Zoho API host and token endpoint: US DC only (zoho.com). Never zoho.eu.
- Shopify API version default `2026-01` is [UNVERIFIED] as current stable; confirm before first run.
- SKU matching is exact and case-sensitive. Mismatched punctuation (e.g. `DSO5102B` vs
  `DSO-5102B`) is skipped, not guessed.
- Items with several Shopify variants sharing a product: first exact SKU match only.
- Zoho list call filters Status.Active; inactive items are not touched.

## Change log
- 04/11/2026: draft script and dependency file created (Claude).
