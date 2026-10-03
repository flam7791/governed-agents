"""The HTTP service: identities and roles, four eyes, background runs, approvals page, metrics."""

import time

import pytest
from fastapi.testclient import TestClient

from govagents.build import mcp_target
from govagents.server import Caller, create_app, parse_tokens

from .conftest import REQUEST, ScriptedLLM, briefing_script

ALICE = "alice-requester-token-0001"
BOB = "bob-approver-token-00000001"
OPS = "ops-admin-token-00000000001"
TOKENS = {
    ALICE: Caller("alice", "requester"),
    BOB: Caller("bob", "approver"),
    OPS: Caller("ops", "admin"),
}


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def service(settings, llm=None, **options):
    llm = llm or ScriptedLLM(briefing_script())
    return TestClient(create_app(settings, TOKENS, llm_for_tier=lambda tier: llm, **options))


def wait_for(client, run_id, status, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}", headers=auth(OPS)).json()
        if run["status"] == status:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} is {run['status']}, expected {status}")


def start(client, token=ALICE):
    response = client.post(
        "/api/runs", headers=auth(token), json={"scenario": "briefing_desk", "request": REQUEST}
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_a_requester_starts_a_run_and_a_named_approver_releases_the_email(settings):
    with service(settings) as c:
        run_id = start(c)
        wait_for(c, run_id, "waiting_approval")
        [pending] = c.get("/api/approvals", headers=auth(BOB)).json()
        assert pending["tool"] == "send_email"
        assert not (settings.data_dir / "outbox").exists()

        refused = c.post(f"/api/approvals/{pending['id']}/approve", headers=auth(ALICE), json={})
        assert refused.status_code == 403  # a requester cannot approve at all

        accepted = c.post(
            f"/api/approvals/{pending['id']}/approve", headers=auth(BOB), json={"note": "ok"}
        )
        assert accepted.status_code == 202 and accepted.json()["by"] == "bob"
        run = wait_for(c, run_id, "completed")

    decided = next(e for e in run["events"] if e["kind"] == "approval_decided")
    assert decided["detail"]["by"] == "bob"  # identity from the token, not from the body
    assert run["requested_by"] == "alice"
    assert len(list((settings.data_dir / "outbox").iterdir())) == 1


def test_whoever_requested_a_run_cannot_approve_its_actions(settings):
    with service(settings) as c:
        run_id = start(c, token=OPS)  # admins may both run and decide...
        wait_for(c, run_id, "waiting_approval")
        [pending] = c.get("/api/approvals", headers=auth(OPS)).json()
        response = c.post(f"/api/approvals/{pending['id']}/approve", headers=auth(OPS), json={})
        assert response.status_code == 403 and "someone else" in response.json()["detail"]
        # ...but not on their own request; another person can.
        c.post(f"/api/approvals/{pending['id']}/approve", headers=auth(BOB), json={})
        wait_for(c, run_id, "completed")


def test_rejection_needs_a_reason_and_a_decision_counts_once(settings):
    with service(settings) as c:
        run_id = start(c)
        wait_for(c, run_id, "waiting_approval")
        [pending] = c.get("/api/approvals", headers=auth(BOB)).json()
        url = f"/api/approvals/{pending['id']}/reject"
        assert c.post(url, headers=auth(BOB), json={"note": " "}).status_code == 422
        assert c.post(url, headers=auth(BOB), json={"note": "Not yet."}).status_code == 202
        assert c.post(url, headers=auth(BOB), json={"note": "Again"}).status_code == 409
        wait_for(c, run_id, "completed")
    assert not (settings.data_dir / "outbox").exists()


def test_tokens_roles_and_the_kill_switch(settings):
    with service(settings) as c:
        assert c.get("/api/runs").status_code == 401
        assert c.get("/api/runs", headers=auth("wrong-token-000000000")).status_code == 401
        assert c.get("/api/me", headers=auth(BOB)).json()["role"] == "approver"
        assert c.post("/api/runs", headers=auth(BOB), json={}).status_code in (403, 422)
        run_id = start(c)
        wait_for(c, run_id, "waiting_approval")
        assert c.post(f"/api/runs/{run_id}/halt", headers=auth(BOB)).status_code == 403
        assert c.post(f"/api/runs/{run_id}/halt", headers=auth(OPS)).status_code == 200
        assert c.get("/api/approvals", headers=auth(BOB)).json() == []
        assert c.get(f"/api/runs/{run_id}", headers=auth(BOB)).json()["status"] == "halted"


def test_unknown_scenarios_and_runs_are_404(settings):
    with service(settings) as c:
        body = {"scenario": "nope", "request": "x"}
        assert c.post("/api/runs", headers=auth(ALICE), json=body).status_code == 404
        assert c.get("/api/runs/run_missing", headers=auth(BOB)).status_code == 404
        assert "briefing_desk" in c.get("/api/scenarios", headers=auth(BOB)).json()


def test_the_approvals_page_never_renders_agent_content_as_html(settings):
    with service(settings) as c:
        page = c.get("/").text
        assert "Agent approvals" in page
        assert "innerHTML" not in page and "textContent" in page
        assert c.get("/healthz").json()["status"] == "ok"


def test_metrics_come_from_the_durable_record(settings):
    with service(settings, metrics_token="scrape-token") as c:
        run_id = start(c)
        wait_for(c, run_id, "waiting_approval")
        [pending] = c.get("/api/approvals", headers=auth(BOB)).json()
        c.post(f"/api/approvals/{pending['id']}/approve", headers=auth(BOB), json={})
        wait_for(c, run_id, "completed")
        assert c.get("/metrics").status_code == 401
        text = c.get("/metrics", headers=auth("scrape-token")).text
    assert 'govagents_runs{scenario="briefing_desk",status="completed"} 1.0' in text
    assert (
        'govagents_policy_decisions_total{agent="dispatcher",tool="publish_to_website",'
        'verdict="deny"} 1.0' in text
    )
    assert 'govagents_approvals{status="approved"} 1.0' in text
    assert 'govagents_model_calls_total{scenario="briefing_desk"}' in text
    assert "govagents_oldest_pending_approval_age_seconds 0.0" in text


def test_tokens_are_parsed_and_checked():
    tokens = parse_tokens("alice:requester:aaaaaaaaaaaaaaaa, bob:approver:bbbbbbbbbbbbbbbb")
    assert tokens["bbbbbbbbbbbbbbbb"] == Caller("bob", "approver")
    with pytest.raises(ValueError, match="role"):
        parse_tokens("eve:superuser:cccccccccccccccc")
    with pytest.raises(ValueError, match="16"):
        parse_tokens("eve:admin:short")
    with pytest.raises(ValueError, match="GOVAGENTS_API_TOKENS"):
        create_app(None, {})


def test_mcp_servers_are_reached_by_url_when_one_is_configured(monkeypatch):
    server = {"name": "evidence", "url_env": "EVIDENCE_URL", "command": "evidence-mcp"}
    monkeypatch.delenv("EVIDENCE_URL", raising=False)
    assert mcp_target(server).command == "evidence-mcp"  # stdio on a laptop
    monkeypatch.setenv("EVIDENCE_URL", "http://evidence-mcp:8000/mcp")
    assert mcp_target(server) == "http://evidence-mcp:8000/mcp"  # HTTP in a deployment


def test_mcp_token_comes_from_the_environment(monkeypatch):
    from govagents.build import mcp_headers

    server = {"name": "evidence", "url_env": "EVIDENCE_URL", "token_env": "EVIDENCE_TOKEN"}
    monkeypatch.delenv("EVIDENCE_TOKEN", raising=False)
    assert mcp_headers(server) is None
    monkeypatch.setenv("EVIDENCE_TOKEN", "emcp_secret")
    assert mcp_headers(server) == {"Authorization": "Bearer emcp_secret"}
    assert mcp_headers({"name": "x"}) is None
