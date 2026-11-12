"""Connector OpenStack: đọc clouds.yaml/openrc, đăng nhập Keystone, chọn endpoint theo catalog, chia nhóm đọc/ghi/quản trị."""
import textwrap

import pytest

import fakes
from kagent import tools
from kagent.connectors import openstack
from kagent.connectors.base import ConnectorError

CLOUDS = textwrap.dedent(f"""
    clouds:
      demo:
        auth:
          auth_url: https://keystone.test:5000
          username: kagent
          password: {fakes.OS_PASSWORD}
          project_name: demo
          user_domain_name: Default
          project_domain_name: Default
        region_name: RegionOne
        interface: public
        identity_api_version: 3
""")

OPENRC = textwrap.dedent("""
    #!/usr/bin/env bash
    export OS_AUTH_URL=https://keystone.test:5000/v3
    export OS_PROJECT_ID=p1
    export OS_PROJECT_NAME="demo"
    export OS_USER_DOMAIN_NAME="Default"
    export OS_USERNAME="kagent"
    echo "Please enter your OpenStack Password for project $OS_PROJECT_NAME as user $OS_USERNAME: "
    read -sr OS_PASSWORD_INPUT
    export OS_PASSWORD=$OS_PASSWORD_INPUT
    export OS_REGION_NAME="RegionOne"
    export OS_INTERFACE=public
""")


def p(**kw):
    return {"_allow_private": False, "config": CLOUDS, **kw}


def test_template_tiers():
    t = tools.load_templates()["openstack"]
    assert {n: s.risk_tier for n, s in t.tools.items()} == {"list_services": "R0", "api_read": "R1", "api_write": "R3", "api_admin": "R4"}


def test_clouds_yaml_login_and_catalog(infra):
    out = openstack.ping(p())
    assert out["project"] == "demo" and out["roles"] == ["member", "reader"] and out["regions"] == ["RegionOne"]
    scope = infra.os_logins[0]["scope"]["project"]
    assert scope == {"name": "demo", "domain": {"name": "Default"}}
    services = {s["type"]: s["url"] for s in openstack.list_services(p(), {})["services"]}
    assert services["compute"] == "https://nova.test/v2.1"  # interface public, không lấy endpoint internal


def test_openrc_needs_password_field(infra):
    with pytest.raises(ConnectorError, match="ô password"):
        openstack.settings({"config": OPENRC})
    out = openstack.api_read({"_allow_private": False, "config": OPENRC, "password": fakes.OS_PASSWORD}, {"service": "nova", "path": "/servers/detail"})
    assert [s["id"] for s in out["json"]["servers"]] == ["s1", "s2"]
    assert infra.os_logins[0]["scope"] == {"project": {"id": "p1"}}


def test_application_credential(infra):
    cfg = textwrap.dedent(f"""
        clouds:
          a:
            auth_type: v3applicationcredential
            auth: {{auth_url: "https://keystone.test/v3", application_credential_id: ac1, application_credential_secret: {fakes.OS_APP_SECRET}}}
          b:
            auth: {{auth_url: "https://keystone.test/v3", username: x, password: y, project_name: z}}
    """)
    with pytest.raises(ConnectorError, match="nhiều cloud"):
        openstack.settings({"config": cfg})
    assert openstack.ping({"_allow_private": False, "config": cfg, "cloud": "a"})["user"] == "kagent"
    assert "scope" not in infra.os_logins[0]


def test_bad_password(infra):
    with pytest.raises(ConnectorError, match="401"):
        openstack.ping(p(password="sai"))


def test_token_cached_and_relogin_on_401(infra):
    openstack.api_read(p(), {"service": "compute", "path": "/servers/detail"})
    openstack.api_read(p(), {"service": "neutron", "path": "/v2.0/ports", "query": {"device_id": "s1"}})
    assert len(infra.os_logins) == 1
    infra.os_tokens.clear()  # token bị thu hồi
    out = openstack.api_read(p(), {"service": "compute", "path": "/servers/detail", "query": {"status": "ERROR"}})
    assert [s["id"] for s in out["json"]["servers"]] == ["s2"] and len(infra.os_logins) == 2


def test_service_aliases_and_microversion(infra):
    out = openstack.api_read(p(), {"service": "cinder", "path": "/volumes/detail", "microversion": "3.60"})
    assert out["service"] == "volumev3" and out["json"]["microversion"] == "volumev3 3.60"
    with pytest.raises(ConnectorError, match="không có dịch vụ heat"):
        openstack.api_read(p(), {"service": "heat", "path": "/stacks"})


def test_profiles(infra):
    action = {"service": "nova", "path": "/servers/s1/action"}
    assert openstack.api_read(p(), {**action, "method": "POST", "body": {"os-getConsoleOutput": {"length": 10}}})["json"]["output"]
    with pytest.raises(ConnectorError, match="api_write"):
        openstack.api_read(p(), {**action, "method": "POST", "body": {"os-stop": None}})
    assert openstack.api_write(p(), {**action, "method": "POST", "body": {"os-stop": None}})["status"] == 202
    for body in ({"rebuild": {"imageRef": "x"}}, {"os-resetState": {"state": "active"}}):
        with pytest.raises(ConnectorError, match="api_admin"):
            openstack.api_write(p(), {**action, "method": "POST", "body": body})
    with pytest.raises(ConnectorError, match="api_admin"):
        openstack.api_write(p(), {"service": "nova", "path": "/servers/s1", "method": "DELETE"})
    with pytest.raises(ConnectorError, match="api_admin"):
        openstack.api_write(p(), {"service": "keystone", "path": "/projects/p1/users/u1/roles/r1", "method": "PUT"})
    with pytest.raises(ConnectorError, match="api_admin"):
        openstack.api_read(p(), {"service": "keystone", "path": "/credentials"})
    assert openstack.api_admin(p(), {"service": "nova", "path": "/servers/s1", "method": "DELETE"})["status"] == 204
    assert "s1" not in infra.os_servers


def test_errors_are_readable(infra):
    with pytest.raises(ConnectorError, match="Instance s9 could not be found"):
        openstack.api_read(p(), {"service": "nova", "path": "/servers/s9"})


@pytest.mark.parametrize("path", ["servers", "/../v3/auth", "//evil.test/x", "https://evil.test/", "/a/%2e%2e/b", "/a b"])
def test_rejects_bad_paths(infra, path):
    with pytest.raises(ConnectorError, match="path"):
        openstack.api_read(p(), {"service": "nova", "path": path})


def test_private_endpoint_blocked(infra):
    with pytest.raises(Exception, match="nội bộ|private|10.0.0.5"):
        openstack.api_read(p(interface="internal"), {"service": "nova", "path": "/servers/detail"})
