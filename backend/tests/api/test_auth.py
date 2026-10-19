from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import PASSWORD, login, make_user


async def test_login_sets_httponly_cookie_and_me_works(
    client: AsyncClient, db: AsyncSession
) -> None:
    await make_user(db, "a@paikane.com", name="Asha")
    resp = await client.post("/auth/login", json={"email": "A@Paikane.com", "password": PASSWORD})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    me = await client.get("/me")
    assert me.status_code == 200
    body = me.json()
    assert body["user"]["email"] == "a@paikane.com"
    assert body["month_spend_usd"] == "0"
    assert "password_hash" not in body["user"]


async def test_wrong_password_rejected(client: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "a@paikane.com")
    resp = await client.post("/auth/login", json={"email": "a@paikane.com", "password": "nope"})
    assert resp.status_code == 401


async def test_login_rate_limited_after_repeated_failures(
    client: AsyncClient, db: AsyncSession
) -> None:
    await make_user(db, "a@paikane.com")
    for _ in range(5):
        await client.post("/auth/login", json={"email": "a@paikane.com", "password": "bad"})
    resp = await client.post("/auth/login", json={"email": "a@paikane.com", "password": PASSWORD})
    assert resp.status_code == 429


async def test_inactive_user_cannot_log_in(client: AsyncClient, db: AsyncSession) -> None:
    user = await make_user(db, "a@paikane.com")
    user.is_active = False
    await db.commit()
    resp = await client.post("/auth/login", json={"email": "a@paikane.com", "password": PASSWORD})
    assert resp.status_code == 401


async def test_logout_clears_session(client: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "a@paikane.com")
    await login(client, "a@paikane.com")
    await client.post("/auth/logout")
    assert (await client.get("/me")).status_code == 401


async def test_unauthenticated_requests_rejected(client: AsyncClient) -> None:
    for method, path in [("GET", "/me"), ("GET", "/conversations"), ("GET", "/models")]:
        assert (await client.request(method, path)).status_code == 401


async def test_only_admin_creates_users(client: AsyncClient, db: AsyncSession) -> None:
    await make_user(db, "member@paikane.com")
    await make_user(db, "admin@paikane.com", role="admin")
    new = {"email": "new@paikane.com", "password": "a-long-enough-password"}

    await login(client, "member@paikane.com")
    assert (await client.post("/admin/users", json=new)).status_code == 403

    await login(client, "admin@paikane.com")
    resp = await client.post("/admin/users", json=new)
    assert resp.status_code == 201
    assert resp.json()["role"] == "member"
    assert (await client.post("/admin/users", json=new)).status_code == 409


async def test_no_self_signup_endpoint(client: AsyncClient) -> None:
    resp = await client.post("/auth/signup", json={"email": "x@example.com", "password": "p" * 12})
    assert resp.status_code in (404, 405)
