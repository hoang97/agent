"""Kết nối Kubernetes bằng kubeconfig: chạy connector thật trên API server giả."""
import base64
import datetime

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import fakes
from kagent import connections, db, net
from kagent.connectors import k8s
from kagent.connectors.base import ConnectorError


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def kubeconfig(user: dict, server="https://k8s.test:6443", cluster_extra=None, current="dev", extra_contexts=()):
    doc = {
        "apiVersion": "v1", "kind": "Config", "current-context": current,
        "clusters": [{"name": "c1", "cluster": {"server": server, **(cluster_extra or {})}}],
        "users": [{"name": "u1", "user": user}],
        "contexts": [{"name": "dev", "context": {"cluster": "c1", "user": "u1"}}, *extra_contexts],
    }
    return yaml.safe_dump(doc)


TOKEN_KC = kubeconfig({"token": fakes.K8S_TOKEN})


def params(**kw):
    return {"_allow_private": False, "namespace": "default", **kw}


def test_kubeconfig_token_lists_pods(infra):
    assert k8s.list_pods(params(kubeconfig=TOKEN_KC), {})["namespace"] == "default"
    assert infra.requests[-1].headers["authorization"] == f"Bearer {fakes.K8S_TOKEN}"


def test_explicit_fields_override_kubeconfig(infra):
    kc = kubeconfig({"token": "wrong-token"}, server="https://127.0.0.1:6443")
    k8s.list_pods(params(kubeconfig=kc, api_url="https://k8s.test:6443", token=fakes.K8S_TOKEN), {})
    assert infra.requests[-1].url.host == "k8s.test" and infra.requests[-1].headers["authorization"] == f"Bearer {fakes.K8S_TOKEN}"


def test_context_selects_cluster_and_user():
    other = {"name": "prod", "context": {"cluster": "c1", "user": "missing"}}
    kc = kubeconfig({"token": "t"}, extra_contexts=[other])
    assert k8s._conn(params(kubeconfig=kc))["token"] == "t"
    with pytest.raises(ConnectorError, match="không có user 'missing'"):
        k8s._conn(params(kubeconfig=kc, context="prod"))


def test_ca_and_insecure_flag_are_read():
    c = k8s._conn(params(kubeconfig=kubeconfig({"token": "t"}, cluster_extra={"certificate-authority-data": b64("PEM-CA")})))
    assert c["ca_cert"] == "PEM-CA" and c["verify_tls"] is True
    assert k8s._conn(params(kubeconfig=kubeconfig({"token": "t"}, cluster_extra={"insecure-skip-tls-verify": True})))["verify_tls"] is False


@pytest.mark.parametrize("user, cluster_extra, message", [
    ({"exec": {"command": "aws", "args": ["eks", "get-token"]}}, None, "lệnh ngoài"),
    ({"auth-provider": {"name": "oidc"}}, None, "lệnh ngoài"),
    ({"token": "t"}, {"certificate-authority": "/home/me/.kube/ca.crt"}, "kubectl config view --raw --minify --flatten"),
    ({"client-certificate": "/x.crt", "client-key": "/x.key"}, None, "tham chiếu file"),
    ({"username": "admin", "password": "p"}, None, "không có token hay chứng chỉ client"),
    ({"client-certificate-data": "%%%", "client-key-data": "%%%"}, None, "base64"),
])
def test_unsupported_kubeconfig_is_explained(user, cluster_extra, message):
    with pytest.raises(ConnectorError, match=message):
        k8s._conn(params(kubeconfig=kubeconfig(user, cluster_extra=cluster_extra)))


def test_missing_current_context_with_several_contexts():
    kc = kubeconfig({"token": "t"}, current=None, extra_contexts=[{"name": "b", "context": {"cluster": "c1", "user": "u1"}}])
    with pytest.raises(ConnectorError, match="điền tham số context"):
        k8s._conn(params(kubeconfig=kc))


def test_invalid_yaml_rejected():
    with pytest.raises(ConnectorError, match="YAML"):
        k8s._conn(params(kubeconfig="clusters: [unterminated"))


def _cert_pair():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kagent-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode())


def test_client_certificate_from_kubeconfig_is_loaded_for_mtls():
    cert, key = _cert_pair()
    c = k8s._conn(params(kubeconfig=kubeconfig({"client-certificate-data": b64(cert), "client-key-data": b64(key)})))
    assert c["client_cert"] == (cert, key) and c["token"] is None
    net.tls_context(True, None, c["client_cert"])  # nạp được vào SSLContext
    with pytest.raises(net.NetError, match="chứng chỉ TLS"):
        net.tls_context(True, None, (cert, "không phải khóa"))


def _admin(conn):
    return conn.execute("SELECT id FROM app_user WHERE username='admin'").fetchone()["id"]


def _create(conn, tenant, **kw):
    return connections.create(conn, tenant, _admin(conn), is_admin=True, is_org_admin=True, template="k8s", scope="tenant",
                              envs=["dev"], roles=["member"], **kw)


def test_connection_with_only_kubeconfig_works_and_secret_stays_hidden(users):
    tenant = users["admin"].tenant
    with db.session() as conn:
        created = _create(conn, tenant, name="kc_dev", params={}, secrets={"kubeconfig": TOKEN_KC})
        assert connections.test(conn, tenant, created["id"], _admin(conn))["ok"] is True
        row = conn.execute("SELECT params, secrets FROM connection WHERE id=?", (created["id"],)).fetchone()
    assert "k8s.test" not in row["params"] and fakes.K8S_TOKEN not in row["secrets"]
    listed = users["admin"].get("/api/connections").json()
    mine = next(c for c in listed if c["name"] == "kc_dev")
    assert mine["secrets_set"]["kubeconfig"] is True and fakes.K8S_TOKEN not in str(listed)


@pytest.mark.parametrize("kw, message", [
    (dict(params={}, secrets={}), "api_url hoặc kubeconfig"),
    (dict(params={"api_url": "https://k8s.test:6443"}, secrets={}), "token"),
    (dict(params={}, secrets={"kubeconfig": kubeconfig({"token": "t"}, server="https://127.0.0.1:6443")}), "127.0.0.1"),
    (dict(params={}, secrets={"kubeconfig": kubeconfig({"exec": {"command": "gcloud"}})}), "lệnh ngoài"),
])
def test_connection_create_validates_kubeconfig(users, kw, message):
    with db.session() as conn, pytest.raises(connections.ConnectionError_, match=message):
        _create(conn, users["admin"].tenant, name="kc_bad", **kw)


def test_secret_sent_in_params_is_stored_as_secret(users):
    tenant = users["admin"].tenant
    with db.session() as conn:
        created = _create(conn, tenant, name="kc_old_client", params={"kubeconfig": TOKEN_KC}, secrets={})
        row = conn.execute("SELECT params, secrets FROM connection WHERE id=?", (created["id"],)).fetchone()
        assert connections.test(conn, tenant, created["id"], _admin(conn))["ok"] is True
    assert "kubeconfig" not in row["params"] and fakes.K8S_TOKEN not in row["secrets"]


def test_static_files_are_revalidated(client):
    r = client.get("/static/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/static/app.js", headers={"if-none-match": r.headers["etag"]}).status_code == 304
