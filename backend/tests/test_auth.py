"""应用级登录模块测试：登录 / 登出 / 鉴权中间件 / 改密 / remember 有效期。

显式 ``monkeypatch.delenv("LAYERLAB_DISABLE_AUTH")`` 关掉 conftest 里的鉴权豁免，
真正走中间件链路。数据目录 / DB 路径 monkeypatch 到 tmp_path（参考 test_api.py）。
"""

from fastapi.testclient import TestClient

from app.main import create_app


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("WB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WB_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.delenv("LAYERLAB_DISABLE_AUTH", raising=False)
    return TestClient(create_app())


def _login(client, username="owner", password="owner123", **extra):
    return client.post("/api/auth/login", json={
        "username": username, "password": password, **extra,
    })


def test_health_is_exempt_from_auth(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_login_success_returns_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = _login(client)
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "owner"
    assert body["token"]
    assert body["expires_at"]


def test_login_wrong_password_returns_401(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = _login(client, password="wrong")
    assert response.status_code == 401


def test_protected_route_requires_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/strategy/defaults")
    assert response.status_code == 401
    assert response.json()["detail"] == "未登录或会话已过期"


def test_valid_bearer_token_grants_access(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    token = _login(client).json()["token"]
    response = client.get(
        "/api/strategy/defaults", headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert len(response.json()["config"]) > 0


def test_remember_me_extends_expiry(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    short = _login(client, remember_me=False).json()["expires_at"]
    long = _login(client, remember_me=True).json()["expires_at"]
    assert long > short


def test_logout_revokes_token(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    token = _login(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_change_password(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    token = _login(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    changed = client.post("/api/auth/change-password", headers=headers, json={
        "current_password": "owner123", "new_password": "new-secret-456",
    })
    assert changed.status_code == 200
    # 旧密码失败
    assert _login(client, password="owner123").status_code == 401
    # 新密码成功
    assert _login(client, password="new-secret-456").status_code == 200


def test_default_admin_seeded_once(tmp_path, monkeypatch):
    """users 表为空时播种 owner/owner123；再次启动不重复播种也不覆盖已改密。"""
    client = _client(tmp_path, monkeypatch)
    assert _login(client, "owner", "owner123").status_code == 200
    # 改密后重启 app：默认播种应跳过（users 非空），旧 owner123 不再可用。
    token = _login(client).json()["token"]
    client.post("/api/auth/change-password", headers={
        "Authorization": f"Bearer {token}",
    }, json={"current_password": "owner123", "new_password": "changed-789"})
    client_restarted = TestClient(create_app())
    assert _login(client_restarted, "owner", "owner123").status_code == 401
    assert _login(client_restarted, "owner", "changed-789").status_code == 200
