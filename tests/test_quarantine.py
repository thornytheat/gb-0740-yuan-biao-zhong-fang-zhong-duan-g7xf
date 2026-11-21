import json

import pytest
from app.modules.m7_release.store import Catalog
from app.modules.m8_quarantine import service

EXPECTED = "sha256:orders-17-expected"


def fault(plan, **override):
    report = {
        "plan": plan,
        "partition": "orders@2026-11-17",
        "evidence": {
            "detector": "checksum-daily",
            "detected": "2026-11-21T02:40:00+00:00",
            "expected": EXPECTED,
            "observed": "sha256:orders-17-corrupt",
        },
        "repairs": [
            {
                "partition": "orders@2026-11-17",
                "version": "s2-fix",
                "rows": 12,
                "checksum": EXPECTED,
            }
        ],
    }
    report.update(override)
    return report


@pytest.fixture
def plan(client):
    return client.post("/api/plans", json={"sample": "nightly"}).json["id"]


@pytest.fixture
def issued(client, plan):
    response = client.post("/api/quarantine", json=fault(plan))
    assert response.status_code == 201
    return response.json["id"]


def document(client, key):
    return client.get(f"/api/quarantine/credential/{key}").json


def test_boundary_covers_multi_hop(client, issued):
    boundary = {row["分区"]: row["跳数"] for row in document(client, issued)["冻结边界"]}
    assert boundary == {
        "orders@2026-11-17": 0,
        "facts@2026-11-17": 1,
        "day0@2026-11-17": 2,
        "day1@2026-11-18": 2,
        "ops@2026-11-17": 3,
        "ops@2026-11-18": 3,
    }
    via = {row["分区"]: row["经自"] for row in document(client, issued)["冻结边界"]}
    assert via["ops@2026-11-17"] == "day0@2026-11-17"  # 三跳间接受影响，未被漏判


def test_release_basis_and_unrecoverable_branch(client, issued):
    doc = document(client, issued)
    basis = {row["分区"]: row["依据"] for row in doc["解除"]}
    assert basis["orders@2026-11-17"] == "REPAIR_VERIFIED"
    assert basis["facts@2026-11-17"] == "RECOMPUTE"
    assert all(row["说明"] and row["目标"] for row in doc["解除"])
    stuck = {row["分区"]: row["原因码"] for row in doc["保持冻结"]}
    assert stuck == {"ops@2026-11-17": "MISSING_INPUT"}
    assert doc["保持冻结"][0]["阻塞上游"] == "day1@2026-11-17"
    assert doc["统计"] == {"边界": 6, "解除": 5, "保持冻结": 1, "未受影响": 53}


def test_same_fault_retry_issues_no_second_credential(client, plan):
    first = client.post("/api/quarantine", json=fault(plan))
    second = client.post("/api/quarantine", json=fault(plan))
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json["id"] == second.json["id"] and second.json["reused"]
    assert len(client.get(f"/api/quarantine/{plan}").json["冻结"]) == 1
    other = client.post("/api/quarantine", json=fault(plan, partition="orders@2026-11-18",
        repairs=[{"partition": "orders@2026-11-18", "version": "s18-fix", "rows": 5, "checksum": EXPECTED}]))
    assert other.status_code == 201 and other.json["id"] != first.json["id"]
    assert len(client.get(f"/api/quarantine/{plan}").json["冻结"]) == 2


def test_release_restores_pre_freeze_versions_on_failure(client, issued):
    doc = document(client, issued)
    drifted = {"facts@2026-11-17": {"version": "foreign", "rows": 99}}
    response = client.post(f"/api/quarantine/credential/{issued}/release", json={"live": drifted})
    assert response.status_code == 422
    body = response.json
    assert body["原因码"] == "STALE_HEAD" and body["失败分区"] == "facts@2026-11-17"
    assert body["恢复一致"] is True
    restored = {row["分区"]: row["恢复版本"] for row in body["已恢复"]}
    assert restored == {"orders@2026-11-17": doc["冻结前快照"]["orders@2026-11-17"]}


def test_release_success_with_consistent_rollback(client, issued):
    response = client.post(f"/api/quarantine/credential/{issued}/release", json={})
    assert response.status_code == 200
    body = response.json
    assert body["状态"] == "已解除" and body["恢复一致"] is True
    moves = {row["分区"]: (row["旧版"], row["新版"]) for row in body["解除"]}
    assert moves["orders@2026-11-17"] == ("s1", "s2-fix")
    assert moves["day1@2026-11-18"][0] is None  # 新增分区回滚时移除
    assert len(moves) == 5


def test_release_replay_after_success_is_reused(client, issued):
    doc = document(client, issued)
    live = {row["分区"]: row["目标"] for row in doc["解除"]}
    response = client.post(f"/api/quarantine/credential/{issued}/release", json={"live": live})
    assert response.json["复用"] is True and response.json["解除"] == []


def test_publish_backfill_review_read_same_credential(client, issued):
    doc = document(client, issued)
    fill = client.get(f"/api/quarantine/credential/{issued}/backfill").json
    assert [row["分区"] for row in fill["补数"]] == [row["分区"] for row in doc["解除"]]
    assert [row["分区"] for row in fill["暂缓"]] == [row["分区"] for row in doc["保持冻结"]]
    audit = client.get(f"/api/quarantine/credential/{issued}/review").json
    assert audit["复核"] == "通过"
    assert [row["分区"] for row in audit["核对"]] == [row["分区"] for row in doc["解除"]]
    assert all(row["结论"] == "一致" for row in audit["核对"])


def test_released_scope_not_auditable_after_evidence_deleted(client, issued):
    doc = document(client, issued)
    evidence = doc["凭证"]["证据编号"]
    assert client.delete(f"/api/quarantine/evidence/{evidence}").status_code == 200
    audit = client.get(f"/api/quarantine/credential/{issued}/review").json
    assert audit["复核"] == "不可复核" and audit["原因码"] == "EVIDENCE_GONE"
    assert set(audit["已解除"]) == {row["分区"] for row in doc["解除"]}
    page = client.get(f"/console/quarantine?plan={doc['凭证']['计划']}&credential={issued}")
    assert "无法复核" in page.text


def test_release_refused_after_evidence_deleted(client, issued):
    doc = document(client, issued)
    client.delete(f"/api/quarantine/evidence/{doc['凭证']['证据编号']}")
    response = client.post(f"/api/quarantine/credential/{issued}/release", json={})
    assert response.status_code == 422 and response.json["code"] == "EVIDENCE_GONE"


def test_unrecoverable_without_repair(client, plan):
    key = client.post("/api/quarantine", json=fault(plan, repairs=[])).json["id"]
    stuck = {row["分区"]: row["原因码"] for row in document(client, key)["保持冻结"]}
    assert stuck["orders@2026-11-17"] == "NO_REPAIR"
    assert stuck["facts@2026-11-17"] == stuck["ops@2026-11-18"] == "UPSTREAM_FROZEN"


def test_repair_checksum_mismatch_stays_frozen(client, plan):
    broken = [{"partition": "orders@2026-11-17", "version": "s2-fix", "rows": 12, "checksum": "sha256:other"}]
    key = client.post("/api/quarantine", json=fault(plan, repairs=broken)).json["id"]
    stuck = {row["分区"]: row["原因码"] for row in document(client, key)["保持冻结"]}
    assert stuck["orders@2026-11-17"] == "REPAIR_MISMATCH"


def test_console_page_shows_boundary_basis_and_stuck(client, plan, issued):
    page = client.get(f"/console/quarantine?plan={plan}&credential={issued}")
    assert page.status_code == 200
    for text in ("冻结边界", "解除依据", "仍不能恢复的分支", "REPAIR_VERIFIED", "MISSING_INPUT"):
        assert text in page.text
    assert client.get(f"/console/quarantine?plan={plan}").status_code == 200


def test_credential_downloadable_for_handoff(client, issued):
    response = client.get(f"/api/quarantine/credential/{issued}")
    assert "attachment" in response.headers["Content-Disposition"]
    assert response.json["凭证"]["编号"] == issued


def test_non_source_partition_rejected(client, plan):
    response = client.post("/api/quarantine", json=fault(plan, partition="facts@2026-11-17",
        repairs=[{"partition": "facts@2026-11-17", "version": "x", "rows": 1, "checksum": EXPECTED}]))
    assert response.status_code == 422 and response.json["code"] == "NOT_SOURCE"


def test_consistent_evidence_is_no_anomaly(client, plan):
    report = fault(plan)
    report["evidence"]["observed"] = report["evidence"]["expected"]
    response = client.post("/api/quarantine", json=report)
    assert response.status_code == 422 and response.json["code"] == "NO_ANOMALY"


def test_foreign_repair_rejected(client, plan):
    repairs = [{"partition": "orders@2026-11-18", "version": "x", "rows": 1, "checksum": EXPECTED}]
    response = client.post("/api/quarantine", json=fault(plan, repairs=repairs))
    assert response.status_code == 422 and response.json["code"] == "REPAIR_FOREIGN"


def test_unknown_plan_rejected(client):
    response = client.post("/api/quarantine", json=fault("missing-plan"))
    assert response.status_code == 422 and response.json["code"] == "PLAN"


def test_tampered_evidence_not_auditable(tmp_path, night):
    store = Catalog(tmp_path / "run.sqlite")
    from app.modules.m7_release.service import execute

    plan, _ = execute(store, night)
    doc, _ = service.issue(store, fault(plan["批次"]["编号"]))
    evidence = doc["凭证"]["证据编号"]
    with store.connection() as db:
        db.execute("UPDATE evidence SET document=? WHERE id=?", (json.dumps({"detector": "x"}), evidence))
    audit = service.review(store, doc["凭证"]["编号"])
    assert audit["复核"] == "不可复核" and audit["原因码"] == "EVIDENCE_CHANGED"


def test_same_fault_single_credential_at_service_level(tmp_path, night):
    store = Catalog(tmp_path / "run.sqlite")
    from app.modules.m7_release.service import execute

    plan, _ = execute(store, night)
    first = service.issue(store, fault(plan["批次"]["编号"]))
    second = service.issue(store, fault(plan["批次"]["编号"]))
    assert first[1] is False and second[1] is True
    assert first[0]["凭证"]["编号"] == second[0]["凭证"]["编号"]
    assert len(store.credentials_for_plan(plan["批次"]["编号"])) == 1
