import json

from app.keys import ReplayError
from app.modules.m8_quarantine import service
from app.web import record, repository
from flask import Blueprint, jsonify, redirect, request, url_for

api = Blueprint("quarantine", __name__)


def credential(key):
    document = repository().get_credential(key)
    if document is None:
        raise ReplayError("CREDENTIAL", "凭证不存在")
    return document


def console_url(document, **extra):
    return url_for(
        "console",
        section="quarantine",
        plan=document["凭证"]["计划"],
        credential=document["凭证"]["编号"],
        **extra,
    )


@api.get("/api/quarantine/<plan>")
def inspection(plan):
    record(plan)
    return jsonify({"冻结": repository().credentials_for_plan(plan)})


@api.post("/api/quarantine")
def create():
    if request.is_json:
        args = request.get_json(silent=True)
        if not isinstance(args, dict):
            raise ReplayError("JSON", "故障报告须为 JSON 对象")
    else:
        form = request.form
        repairs_text = (form.get("repairs") or "").strip()
        try:
            repairs = json.loads(repairs_text) if repairs_text else []
        except ValueError as exc:
            raise ReplayError("REPAIR", "修复快照须为 JSON 数组") from exc
        args = {
            "plan": form.get("plan"),
            "partition": form.get("partition"),
            "evidence": {
                "detector": form.get("detector"),
                "detected": form.get("detected"),
                "expected": form.get("expected"),
                "observed": form.get("observed"),
            },
            "repairs": repairs,
        }
    document, reused = service.issue(repository(), args)
    if not request.is_json:
        return redirect(console_url(document, creused="yes" if reused else None), 303)
    return jsonify(
        id=document["凭证"]["编号"],
        reused=reused,
        统计=document["统计"],
        next=console_url(document),
    ), 200 if reused else 201


@api.get("/api/quarantine/credential/<key>")
def download(key):
    response = jsonify(credential(key))
    response.headers.set("Content-Disposition", "attachment", filename=f"credential-{key}.json")
    return response


@api.post("/api/quarantine/credential/<key>/release")
def release(key):
    credential(key)
    live = None
    if request.is_json:
        args = request.get_json(silent=True)
        if not isinstance(args, dict):
            raise ReplayError("JSON", "请求须为 JSON 对象")
        live = args.get("live")
    result = service.execute_release(repository(), key, live)
    if not request.is_json:
        if result["状态"] == "解除失败":
            return redirect(console_url(credential(key), failed=result["失败分区"]), 303)
        return redirect(console_url(credential(key), released="yes"), 303)
    return jsonify(result), 422 if result["状态"] == "解除失败" else 200


@api.get("/api/quarantine/credential/<key>/backfill")
def fill(key):
    credential(key)
    return jsonify(service.backfill(repository(), key))


@api.get("/api/quarantine/credential/<key>/review")
def audit(key):
    credential(key)
    return jsonify(service.review(repository(), key))


@api.delete("/api/quarantine/evidence/<key>")
def drop(key):
    if not repository().drop_evidence(key):
        raise ReplayError("EVIDENCE", "证据不存在")
    return jsonify(deleted=key)


@api.post("/api/quarantine/evidence/<key>/drop")
def drop_form(key):
    if not repository().drop_evidence(key):
        raise ReplayError("EVIDENCE", "证据不存在")
    plan = request.form.get("plan")
    credential_key = request.form.get("credential")
    return redirect(
        url_for("console", section="quarantine", plan=plan, credential=credential_key), 303
    )
