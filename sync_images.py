#!/usr/bin/env python3
"""
Zoho Books <- Shopify item image sync (Debug Store).

For each active Zoho Books item with no image, find the Shopify variant with the
same SKU, take the product's featured image, and upload it to the Zoho item.

Default is DRY RUN: nothing is written to Zoho. Pass --apply to upload.
Items with no SKU match or no Shopify image are skipped and reported.
Exit code 1 if any upload failed or any read failed (FAIL-1).

Credentials come only from environment variables (GitHub Secrets in production):
  ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_ORG_ID
  SHOPIFY_SHOP          shop subdomain only, without .myshopify.com
  SHOPIFY_CLIENT_ID     Dev Dashboard app Client ID
  SHOPIFY_CLIENT_SECRET Dev Dashboard app Client secret
  (Token is fetched each run by the client credentials grant; it lasts 24 hours.)
Optional:
  ZOHO_ACCOUNTS_HOST    default accounts.zoho.com (US data centre; never zoho.eu)
  ZOHO_API_HOST         default www.zohoapis.com (US data centre)
  SHOPIFY_API_VERSION   default 2026-01 [UNVERIFIED: confirm current stable version]
  REPORT_DIR            default ./reports

Stdlib only, so no dependency install is needed.

UNVERIFIED: the Zoho Books item image upload endpoint. A forum post cites
POST /books/v3/items/{item_id}/image with multipart field "image" and
organization_id as a query parameter. Official docs did not show it. Upload is
disabled until UPLOAD_ENDPOINT_VERIFIED is set True after a live test on one item.
"""
import argparse
import csv
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

UPLOAD_ENDPOINT_VERIFIED = False  # flip to True only after a live test on one item
ZOHO_PAGE_SIZE = 200
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # [UNVERIFIED] Zoho item image size limit; confirm


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing required environment variable: {name}")
    return v


def http(method, url, headers=None, data=None, timeout=60, retries=3):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            return e.code, e.read(), e.headers
        except urllib.error.URLError as e:
            last = e
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"Request failed after {retries} attempts: {last}")


class Zoho:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.api = f"https://{cfg['api_host']}/books/v3"

    def _refresh(self):
        q = urllib.parse.urlencode({
            "refresh_token": self.cfg["refresh_token"],
            "client_id": self.cfg["client_id"],
            "client_secret": self.cfg["client_secret"],
            "grant_type": "refresh_token",
        })
        status, body, _ = http("POST", f"https://{self.cfg['accounts_host']}/oauth/v2/token?{q}")
        data = json.loads(body or b"{}")
        if status != 200 or "access_token" not in data:
            sys.exit(f"Zoho token refresh failed (HTTP {status}): {data}")
        self.token = data["access_token"]

    def _headers(self):
        if not self.token:
            self._refresh()
        return {"Authorization": f"Zoho-oauthtoken {self.token}"}

    def get(self, path, params):
        params = dict(params, organization_id=self.cfg["org_id"])
        url = f"{self.api}{path}?{urllib.parse.urlencode(params)}"
        status, body, _ = http("GET", url, headers=self._headers())
        data = json.loads(body or b"{}")
        if status != 200 or data.get("code", 0) != 0:
            raise RuntimeError(f"Zoho GET {path} failed (HTTP {status}): {data.get('message')}")
        return data

    def list_items(self):
        page, items = 1, []
        while True:
            d = self.get("/items", {"page": page, "per_page": ZOHO_PAGE_SIZE, "filter_by": "Status.Active"})
            items.extend(d.get("items", []))
            if not d.get("page_context", {}).get("has_more_page"):
                return items
            page += 1

    def upload_image(self, item_id, filename, content, mime):
        """Attach an image to an item. Two routes are tried in order:
        A. PUT /itemmasters/{id} with form-data "image" (documented in the Zoho Books
           items API, scope ZohoBooks.items.UPDATE).
        B. POST /items/{id}/image with form-data "image" (community-reported route).
        Guarded by UPLOAD_ENDPOINT_VERIFIED unless in one-SKU test mode."""
        if not (UPLOAD_ENDPOINT_VERIFIED or getattr(self, "test_mode", False)):
            raise RuntimeError("Upload endpoint not verified; refusing to write.")
        boundary = uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="JSONString"\r\n\r\n'
            f"{{}}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        q = urllib.parse.urlencode({"organization_id": self.cfg["org_id"]})
        routes = [("PUT", f"{self.api}/itemmasters/{item_id}?{q}", "A itemmasters PUT"),
                  ("POST", f"{self.api}/items/{item_id}/image?{q}", "B items/image POST")]
        errors = []
        for method, url, label in routes:
            headers = self._headers()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            status, resp, _ = http(method, url, headers=headers, data=body)
            try:
                data = json.loads(resp or b"{}")
            except ValueError:
                data = {"message": (resp or b"")[:200].decode("utf-8", "replace")}
            if status in (200, 201) and data.get("code", 0) == 0:
                data["_route"] = label
                return data
            errors.append(f"{label}: HTTP {status} {data.get('code')} {data.get('message')}")
        raise RuntimeError("Zoho image upload failed. " + " | ".join(errors))


class Shopify:
    def __init__(self, cfg):
        base = f"https://{cfg['shop']}.myshopify.com"
        self.url = f"{base}/admin/api/{cfg['version']}/graphql.json"
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
        }).encode()
        status, resp, _ = http("POST", f"{base}/admin/oauth/access_token",
                               headers={"Content-Type": "application/x-www-form-urlencoded"}, data=body)
        try:
            data = json.loads(resp or b"{}")
        except ValueError:
            data = {"non_json_body": (resp or b"")[:300].decode("utf-8", "replace")}
        if status != 200 or "access_token" not in data:
            sys.exit(f"Shopify token request failed (HTTP {status}): {data}")
        self.headers = {
            "X-Shopify-Access-Token": data["access_token"],
            "Content-Type": "application/json",
        }

    def gql(self, query, variables):
        body = json.dumps({"query": query, "variables": variables}).encode()
        status, resp, _ = http("POST", self.url, headers=self.headers, data=body)
        data = json.loads(resp or b"{}")
        if status != 200 or data.get("errors"):
            raise RuntimeError(f"Shopify GraphQL failed (HTTP {status}): {data.get('errors', data)}")
        return data["data"]

    QUERY = """
    query($q: String!) {
      productVariants(first: 5, query: $q) {
        edges { node { sku product { title status featuredMedia {
          ... on MediaImage { image { url } } } } } }
      }
    }"""

    def image_for_sku(self, sku):
        """Exact, case-sensitive SKU match among active products.
        Returns (title, image_url) or (None, None) if no usable match."""
        # Quote the SKU so punctuation such as '.' or '-' is matched literally.
        q = f'sku:"{sku}"'
        edges = self.gql(self.QUERY, {"q": q})["productVariants"]["edges"]
        for e in edges:
            n = e["node"]
            if n["sku"] == sku and n["product"]["status"] == "ACTIVE":
                media = n["product"].get("featuredMedia") or {}
                url = (media.get("image") or {}).get("url")
                return n["product"]["title"], url
        return None, None


def download(url):
    status, body, headers = http("GET", url, timeout=120)
    if status != 200:
        raise RuntimeError(f"Image download failed (HTTP {status})")
    if len(body) > MAX_IMAGE_BYTES:
        raise RuntimeError(f"Image too large ({len(body)} bytes)")
    mime = headers.get("Content-Type", "").split(";")[0] or mimetypes.guess_type(url)[0] or "image/jpeg"
    ext = mimetypes.guess_extension(mime) or ".jpg"
    return body, mime, ext


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write images to Zoho (default: dry run)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N candidate items (0 = all)")
    ap.add_argument("--test-sku", default="", help="upload for this one SKU only, to verify the Zoho endpoint")
    args = ap.parse_args()

    zcfg = {
        "client_id": env("ZOHO_CLIENT_ID", required=True),
        "client_secret": env("ZOHO_CLIENT_SECRET", required=True),
        "refresh_token": env("ZOHO_REFRESH_TOKEN", required=True),
        "org_id": env("ZOHO_ORG_ID", required=True),
        "accounts_host": env("ZOHO_ACCOUNTS_HOST", "accounts.zoho.com"),
        "api_host": env("ZOHO_API_HOST", "www.zohoapis.com"),
    }
    scfg = {
        "shop": env("SHOPIFY_SHOP", required=True),
        "client_id": env("SHOPIFY_CLIENT_ID", required=True),
        "client_secret": env("SHOPIFY_CLIENT_SECRET", required=True),
        "version": env("SHOPIFY_API_VERSION", "2026-01"),
    }
    if args.test_sku:
        args.apply = True
    elif args.apply and not UPLOAD_ENDPOINT_VERIFIED:
        sys.exit("Refusing --apply: Zoho upload endpoint is not yet verified (UPLOAD_ENDPOINT_VERIFIED = False).")

    zoho, shop = Zoho(zcfg), Shopify(scfg)
    zoho.test_mode = bool(args.test_sku)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = env("REPORT_DIR", "reports")
    os.makedirs(report_dir, exist_ok=True)

    items = zoho.list_items()
    candidates = [i for i in items if not i.get("image_document_id") and not i.get("image_name")]
    if args.test_sku:
        candidates = [i for i in candidates if (i.get("sku") or "").strip() == args.test_sku]
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"Zoho active items: {len(items)}; without image: {len(candidates)}")

    rows, counts = [], {"matched": 0, "uploaded": 0, "skipped": 0, "failed": 0}
    for it in candidates:
        sku = (it.get("sku") or "").strip()
        row = {"item_id": it["item_id"], "sku": sku, "name": it.get("name", "")[:80],
               "shopify_title": "", "status": "", "detail": ""}
        try:
            if not sku:
                row.update(status="skipped", detail="no SKU in Zoho")
            else:
                title, img_url = shop.image_for_sku(sku)
                if not title:
                    row.update(status="skipped", detail="no Shopify variant with this SKU")
                elif not img_url:
                    row.update(status="skipped", detail="Shopify product has no image", shopify_title=title)
                else:
                    counts["matched"] += 1
                    row["shopify_title"] = title
                    if not args.apply:
                        row.update(status="dry_run", detail=img_url)
                    else:
                        content, mime, ext = download(img_url)
                        res = zoho.upload_image(it["item_id"], f"{sku}{ext}", content, mime)
                        # Read back: the upload must show on the item.
                        back = zoho.get(f"/items/{it['item_id']}", {})["item"]
                        if not back.get("image_document_id") and not back.get("image_name"):
                            raise RuntimeError("read-back shows no image after upload")
                        row.update(status="uploaded", detail=f"{res.get('_route')}; image_document_id={back.get('image_document_id')}",
                                   shopify_title=title)
        except Exception as e:  # keep going, record the failure
            row.update(status="failed", detail=str(e))
        if row["status"] in ("skipped", "failed", "uploaded"):
            counts[row["status"]] += 1
        rows.append(row)
        print(f"{row['status']:>8}  {sku or '(no SKU)'}  {row['detail'][:400]}")

    csv_path = os.path.join(report_dir, f"image-sync-{run_id}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["item_id"])
        w.writeheader()
        w.writerows(rows)
    summary = {"run_id": run_id, "mode": "apply" if args.apply else "dry_run",
               "zoho_active_items": len(items), "candidates": len(candidates), **counts,
               "report": csv_path}
    with open(os.path.join(report_dir, f"image-sync-{run_id}.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
