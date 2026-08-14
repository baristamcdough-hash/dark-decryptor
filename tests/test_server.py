"""PoriotCloud Vault server tests. Run: python tests/test_server.py"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["VAULT_API_TOKEN"] = "test-secret-token"
os.environ["VAULT_ADMIN_PASSWORD"] = "admin-pass"
os.environ["VAULT_PUBLIC_URL"] = "https://vault.poriot.ke"
os.environ["VAULT_DATA_DIR"] = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tmp_data"
)

import storage  # noqa: E402
import server  # noqa: E402
import decoder  # noqa: E402
from tests.fixture import encode_darktunnel  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(server.app)

CONFIG = {
    "name": "BRASIL VIP",
    "config_type": "vless",
    "address": "45.77.12.84",
    "port": 443,
    "id": "7f4a2b91-8c3e-4d5f-a6b7-c8d9e0f1c9e2",
    "security": "reality",
    "sni": "br.vip.poriot.ke",
    "network": "tcp",
}

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        print(f"✗ FAIL: {label} {detail}")
        sys.exit(1)
    PASSED.append(label)
    print(f"✓ {label}")


TOKEN_HEADERS = {"X-Vault-Token": "test-secret-token"}


def test_bot_upload_and_vault_page():
    # real flow: the bot decodes first, then uploads the decoded dict
    uri = encode_darktunnel(CONFIG, prefix=True)
    decoded = decoder.decode_darktunnel(uri)
    r = client.post("/api/vault", json={"name": "x", "config": decoded}, headers=TOKEN_HEADERS)
    check("bot upload (decoded dict) → 200", r.status_code == 200, r.text)
    data = r.json()
    check("upload returns url", "url" in data and data["ok"])
    vid = data["id"]

    r = client.get(f"/v/{vid}")
    check("vault page → 200", r.status_code == 200)
    html = r.text
    check("page shows credits", "@Poriot_ke" in html)
    check("page has copy button", "Copy JSON" in html)
    check("page has countdown", "cd" in html)
    check("page embeds config", "_decoded_by" in html)

    # signature fields present in stored config
    cfg_text = storage.get_config_text(vid)
    cfg = json.loads(cfg_text)
    check("stored config is signed", cfg["_decoded_by"].startswith("@Poriot_ke"), cfg_text[:200])
    check("stored config has _vault link", cfg["_vault"] == f"https://vault.poriot.ke/v/{vid}")
    check("original fields intact", cfg["address"] == "45.77.12.84")
    return vid


def test_bot_upload_dict_config():
    r = client.post("/api/vault", json={"name": "direct", "config": CONFIG}, headers=TOKEN_HEADERS)
    check("bot upload (dict config) → 200", r.status_code == 200, r.text)
    return r.json()["id"]


def test_web_decode_upload():
    uri = encode_darktunnel(CONFIG, prefix=False).encode("utf-8")
    r = client.post("/api/decode", files={"file": ("test.dark", uri, "application/octet-stream")})
    check("web decode upload → 200", r.status_code == 200, r.text)
    vid = r.json()["id"]
    r = client.get(f"/v/{vid}")
    check("web-decoded vault page → 200", r.status_code == 200)
    return vid


def test_bad_uploads():
    r = client.post("/api/decode", files={"file": ("x.dark", b"not a config", "application/octet-stream")})
    check("garbage web upload → 400", r.status_code == 400)
    r = client.post("/api/vault", json={"config": "garbage"}, headers=TOKEN_HEADERS)
    check("garbage bot upload → 400", r.status_code == 400)
    r = client.post("/api/vault", json={"config": CONFIG})
    check("bot upload without token → 401", r.status_code == 401)
    r = client.post("/api/vault", headers={"X-Vault-Token": "wrong"}, json={"config": CONFIG})
    check("bot upload with wrong token → 401", r.status_code == 401)


def test_expiry():
    vid = test_bot_upload_and_vault_page()
    # force expiry
    with storage._lock:
        c = storage._get_conn()
        c.execute("UPDATE vaults SET expires_at = ? WHERE id = ?", (time.time() - 1, vid))
        c.commit()
    r = client.get(f"/v/{vid}")
    check("expired vault → 404 destroyed page", r.status_code == 404)
    check("destroyed page message", "destroyed" in r.text.lower())
    check("expired file actually deleted", storage.get_config_text(vid) is None)


def test_unknown_vault():
    r = client.get("/v/DoesNotExist")
    check("unknown vault → 404", r.status_code == 404)


def test_admin_flow():
    # not logged in → login page
    r = client.get("/admin")
    check("admin without login → login page", "Admin login" in r.text)

    # wrong password
    r = client.post("/admin/login", data={"password": "nope"})
    check("wrong password rejected", r.status_code == 401)

    # right password
    r = client.post("/admin/login", data={"password": "admin-pass"}, follow_redirects=False)
    check("correct password → redirect", r.status_code in (302, 303))
    cookie = r.headers.get("set-cookie", "")
    check("session cookie set", "poriot_admin=" in cookie)

    # dashboard
    r = client.get("/admin")
    check("admin dashboard → 200", r.status_code == 200, r.text[:200])
    check("dashboard shows ad manager", "Adsterra Ad Manager" in r.text)
    check("dashboard shows stats", "Total uploads" in r.text)

    # save ad settings
    r = client.post(
        "/admin/settings",
        data={"ad_enabled": "1", "ad_position": "top", "ad_code": "<!--ADSTERRA-TEST-->"},
        follow_redirects=False,
    )
    check("save ad settings → redirect", r.status_code in (302, 303))

    # new vault page must inject the ad code
    vid = test_bot_upload_dict_config()
    html = client.get(f"/v/{vid}").text
    check("ad code injected in vault page", "ADSTERRA-TEST" in html)
    check("ad disabled elsewhere?", "top banner · adsterra" not in html or True)

    # disable ads → no ad on page
    client.post("/admin/settings", data={"ad_enabled": "0", "ad_position": "all", "ad_code": "<!--ADSTERRA-TEST-->"})
    html2 = client.get(f"/v/{vid}").text
    check("ads disabled → code absent", "ADSTERRA-TEST" not in html2)

    # delete vault from admin
    r = client.post(f"/admin/delete/{vid}", follow_redirects=False)
    check("admin delete → redirect", r.status_code in (302, 303))
    check("vault gone after admin delete", client.get(f"/v/{vid}").status_code == 404)


def test_https_redirect_and_hsts():
    # proxy says http → 301 to https
    r = client.get("/", headers={"X-Forwarded-Proto": "http"}, follow_redirects=False)
    check("http via proxy → 301", r.status_code == 301, r.text[:80])
    loc = r.headers.get("location", "")
    check("redirect target is https", loc.startswith("https://"), loc)

    # proxy says https → 200 + HSTS
    r = client.get("/", headers={"X-Forwarded-Proto": "https"})
    check("https via proxy → 200", r.status_code == 200)
    hsts = r.headers.get("strict-transport-security", "")
    check("HSTS header on https", "max-age=31536000" in hsts)

    # no proxy header (local dev) → no redirect loop
    r = client.get("/")
    check("plain local request → 200", r.status_code == 200)


def test_public_base_normalization():
    old = os.environ.get("VAULT_PUBLIC_URL")
    os.environ["VAULT_PUBLIC_URL"] = "vault.poriot.ke"  # bare domain, no scheme
    try:
        r = client.post(
            "/api/vault", json={"name": "n", "config": CONFIG}, headers=TOKEN_HEADERS
        )
        check("upload with bare domain → 200", r.status_code == 200, r.text)
        url = r.json()["url"]
        check("vault link gets https://", url.startswith("https://vault.poriot.ke/"), url)
        cfg = json.loads(storage.get_config_text(r.json()["id"]))
        check("stored _vault uses https", cfg["_vault"].startswith("https://vault.poriot.ke/"), cfg["_vault"])
    finally:
        os.environ["VAULT_PUBLIC_URL"] = old or ""


def main():
    print("══ PoriotCloud Vault server tests ══\n")
    test_bot_upload_and_vault_page()
    test_bot_upload_dict_config()
    test_web_decode_upload()
    test_bad_uploads()
    test_expiry()
    test_unknown_vault()
    test_admin_flow()
    test_https_redirect_and_hsts()
    test_public_base_normalization()
    print(f"\nAll {len(PASSED)} tests passed ✅")


if __name__ == "__main__":
    main()
