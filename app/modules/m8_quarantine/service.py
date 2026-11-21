import hashlib
import json
from collections import deque
from datetime import datetime, timedelta, timezone

from app.config import VERSION
from app.keys import ReplayError, day, identifier, nonnegative_count, partition
from app.modules.m2_graph.service import reverse_edges, topology
from app.modules.m3_fingerprint.service import destination, fingerprint
from app.modules.m7_release.service import safe_counts

EVIDENCE_FIELDS = {"detector", "detected", "expected", "observed"}


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def as_int(value):
    # 指针与凭证中的行数可能因超出安全整数范围而以字符串登记。
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ReplayError("POINTER", "指针行数无效")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ReplayError("POINTER", "指针行数无效") from None


def evidence_key(document):
    return "ev" + hashlib.sha256(canon(document).encode()).hexdigest()[:24]


def credential_key(plan, report):
    payload = canon({"计划": plan, "故障": report})
    return "q" + hashlib.sha256(f"{VERSION}:{payload}".encode()).hexdigest()[:25]


def parse_partition(text):
    if not isinstance(text, str) or text.count("@") != 1:
        raise ReplayError("PARTITION", "分区须为 数据集@YYYY-MM-DD")
    name, date_text = text.split("@")
    try:
        return partition(name, date_text)
    except ValueError as exc:
        raise ReplayError("PARTITION", "分区须为 数据集@YYYY-MM-DD") from exc


def parse_evidence(raw):
    if not isinstance(raw, dict) or set(raw) != EVIDENCE_FIELDS:
        raise ReplayError("EVIDENCE", "证据须含 detector/detected/expected/observed")
    try:
        detector = identifier(raw["detector"])
    except (ValueError, TypeError) as exc:
        raise ReplayError("EVIDENCE", "检测器标识无效") from exc
    detected = raw["detected"]
    if not isinstance(detected, str):
        raise ReplayError("EVIDENCE", "检测时间须为 ISO 8601")
    try:
        datetime.fromisoformat(detected.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayError("EVIDENCE", "检测时间须为 ISO 8601") from exc
    expected, observed = raw["expected"], raw["observed"]
    for label, value in (("期望校验值", expected), ("实测校验值", observed)):
        if not isinstance(value, str) or not 1 <= len(value) <= 200:
            raise ReplayError("EVIDENCE", f"{label}须为 1-200 字符")
    if expected == observed:
        raise ReplayError("NO_ANOMALY", "期望与实测一致，无异常可处置")
    return {
        "detector": detector,
        "detected": detected,
        "expected": expected,
        "observed": observed,
    }


def parse_repairs(raw, fault_part):
    if raw is None:
        return {}
    if not isinstance(raw, list) or len(raw) > 300:
        raise ReplayError("REPAIR", "修复快照须为不超过 300 条的数组")
    out = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ReplayError("REPAIR", "修复快照无效")
        try:
            part = parse_partition(item.get("partition"))
            version = identifier(item.get("version"))
            rows = nonnegative_count(item.get("rows"))
            checksum = item.get("checksum")
        except (ValueError, TypeError) as exc:
            raise ReplayError("REPAIR", "修复快照的版本、行数或分区无效") from exc
        if not isinstance(checksum, str) or not 1 <= len(checksum) <= 200:
            raise ReplayError("REPAIR", "修复快照校验值须为 1-200 字符")
        if part != fault_part:
            raise ReplayError("REPAIR_FOREIGN", "修复快照须针对异常源分区")
        if part in out:
            raise ReplayError("REPAIR_DUP", "同一分区的修复快照重复")
        out[part] = {"version": version, "rows": rows, "checksum": checksum}
    return out


def freeze_boundary(graph, part, through):
    """从异常源分区沿反向依赖做多跳扩散，圈出截止日内的全部冻结分区。"""
    limit = day(through)
    edges = reverse_edges(graph)
    name, date_text = part.split("@")
    seen = {part: (0, None)}
    queue = deque([(name, day(date_text))])
    while queue:
        node, when = queue.popleft()
        depth = seen[f"{node}@{when.isoformat()}"][0]
        for child, lag in edges[node]:
            next_day = when + timedelta(days=lag)
            if next_day > limit:
                continue
            key = f"{child}@{next_day.isoformat()}"
            if key in seen:
                continue
            seen[key] = (depth + 1, f"{node}@{when.isoformat()}")
            queue.append((child, next_day))
    ordered = sorted(seen, key=lambda k: (seen[k][0], k))
    return [{"分区": key, "跳数": seen[key][0], "经自": seen[key][1]} for key in ordered]


def decide(graph, heads, frozen, part, repairs, evidence):
    """按拓扑序逐分区判定：源分区凭修复校验值解除，下游凭上游处置结果重算。"""
    order = topology(graph)
    index = {name: i for i, name in enumerate(order)}
    frozen_keys = {entry["分区"] for entry in frozen}
    released, stuck, targets = [], [], {}
    for key in sorted(frozen_keys, key=lambda k: (index[k.split("@")[0]], k.split("@")[1])):
        name, date_text = key.split("@")
        current = heads.get(key)
        if key == part:
            repair = repairs.get(key)
            if repair is None:
                stuck.append({"分区": key, "原因码": "NO_REPAIR", "说明": "尚无修复快照"})
                continue
            if repair["checksum"] != evidence["expected"]:
                stuck.append(
                    {
                        "分区": key,
                        "原因码": "REPAIR_MISMATCH",
                        "说明": f"修复校验值 {repair['checksum']} 与期望 {evidence['expected']} 不符",
                    }
                )
                continue
            target = {"version": repair["version"], "rows": repair["rows"]}
            targets[key] = target
            released.append(
                {
                    "分区": key,
                    "依据": "REPAIR_VERIFIED",
                    "说明": f"修复快照 {repair['version']} 校验值与期望一致（{evidence['detector']}）",
                    "修订": graph[name]["revision"],
                    "当前": current,
                    "目标": target,
                    "上游": {},
                    "修复校验值": repair["checksum"],
                }
            )
            continue
        inputs = {}
        blocker = None
        for parent, lag in graph[name]["reads"]:
            parent_day = (day(date_text) - timedelta(days=lag)).isoformat()
            parent_key = f"{parent}@{parent_day}"
            if parent_key in frozen_keys:
                if parent_key in targets:
                    inputs[parent_key] = targets[parent_key]
                else:
                    blocker = ("UPSTREAM_FROZEN", parent_key, f"上游 {parent_key} 仍未解除")
                    break
            else:
                snapshot = heads.get(parent_key)
                if snapshot is None:
                    blocker = ("MISSING_INPUT", parent_key, f"非冻结上游 {parent_key} 缺快照")
                    break
                inputs[parent_key] = snapshot
        if blocker:
            code, ref, note = blocker
            stuck.append({"分区": key, "原因码": code, "说明": note, "阻塞上游": ref})
            continue
        target = destination(name, graph[name]["revision"], date_text, inputs)
        targets[key] = target
        basis = "、".join(
            f"{source} {'已解除' if source in frozen_keys else '未受影响'}" for source in inputs
        )
        released.append(
            {
                "分区": key,
                "依据": "RECOMPUTE",
                "说明": f"按拓扑重算：{basis}",
                "修订": graph[name]["revision"],
                "当前": current,
                "目标": target,
                "上游": inputs,
            }
        )
    return released, stuck


def issue(store, args):
    if not isinstance(args, dict):
        raise ReplayError("JSON", "故障报告须为 JSON 对象")
    plan_key = args.get("plan")
    plan = store.get(plan_key) if isinstance(plan_key, str) else None
    if plan is None:
        raise ReplayError("PLAN", "计划不存在，请先生成重放计划")
    graph, heads = plan.get("依赖图"), plan.get("指针")
    if not graph or heads is None:
        raise ReplayError("PLAN_OLD", "计划缺少依赖图或指针，请重新生成计划")
    heads = {
        key: {"version": value["version"], "rows": as_int(value["rows"])}
        for key, value in heads.items()
    }
    through = plan["批次"]["截止分区日"]
    part = parse_partition(args.get("partition"))
    name, date_text = part.split("@")
    if name not in graph or part not in heads:
        raise ReplayError("UNKNOWN_PARTITION", "异常分区不在计划指针内")
    if graph[name]["reads"]:
        raise ReplayError("NOT_SOURCE", "仅源表分区可登记校验异常")
    if day(date_text) > day(through):
        raise ReplayError("DATE_LIMIT", "异常分区晚于计划截止日")
    evidence = parse_evidence(args.get("evidence"))
    repairs = parse_repairs(args.get("repairs"), part)
    report = {
        "partition": part,
        "evidence": evidence,
        "repairs": [repairs[key] for key in sorted(repairs)],
    }
    key = credential_key(plan_key, report)
    existing = store.get_credential(key)
    if existing:
        return existing, True
    frozen = freeze_boundary(graph, part, through)
    released, stuck = decide(graph, heads, frozen, part, repairs, evidence)
    ev_key = evidence_key(evidence)
    document = {
        "凭证": {
            "编号": key,
            "计划": plan_key,
            "规则": VERSION,
            "时区": "UTC",
            "签发时间": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "故障分区": part,
            "截止分区日": through,
            "证据编号": ev_key,
            "证据摘要": ev_key,
            "检测器": evidence["detector"],
        },
        "冻结边界": frozen,
        "解除": released,
        "保持冻结": stuck,
        "冻结前快照": {entry["分区"]: heads.get(entry["分区"]) for entry in frozen},
        "统计": {
            "边界": len(frozen),
            "解除": len(released),
            "保持冻结": len(stuck),
            "未受影响": len(heads) - len(frozen),
        },
    }
    document = safe_counts(document)
    store.save_evidence(ev_key, evidence)
    return store.save_credential(key, plan_key, document)[0], False


def view(store, key):
    document = store.get_credential(key)
    if document is None:
        return None
    evidence = store.get_evidence(document["凭证"]["证据编号"])
    return {**document, "证据在场": evidence is not None, "证据": evidence}


def _parse_live(raw, heads, frozen):
    if raw is None:
        return dict(heads)
    if not isinstance(raw, dict) or len(raw) > 300:
        raise ReplayError("LIVE", "现行指针须为对象")
    known = set(heads) | set(frozen)
    live = dict(heads)
    for text, value in raw.items():
        part = parse_partition(text)
        if part not in known:
            raise ReplayError("LIVE", f"现行指针含未知分区 {part}")
        if not isinstance(value, dict):
            raise ReplayError("LIVE", "现行指针无效")
        try:
            live[part] = {
                "version": identifier(value.get("version")),
                "rows": nonnegative_count(value.get("rows")),
            }
        except (ValueError, TypeError) as exc:
            raise ReplayError("LIVE", "现行指针的版本或行数无效") from exc
    return live


def execute_release(store, key, raw_live):
    """按凭证演练解除；任一前置失效时恢复冻结前旧版结果再报失败。"""
    document = store.get_credential(key)
    if document is None:
        raise ReplayError("CREDENTIAL", "凭证不存在")
    if store.get_evidence(document["凭证"]["证据编号"]) is None:
        raise ReplayError("EVIDENCE_GONE", "原异常证据已删除，无法解除")
    plan = store.get(document["凭证"]["计划"])
    frozen = document["冻结前快照"]
    live = _parse_live(raw_live, plan["指针"], frozen)
    moves = [(entry["分区"], frozen.get(entry["分区"]), entry["目标"]) for entry in document["解除"]]
    if not moves:
        return {"凭证": key, "状态": "无可解除", "复用": True, "解除": [], "恢复一致": True}
    if all(live.get(part) == target for part, _, target in moves):
        return {"凭证": key, "状态": "已解除", "复用": True, "解除": [], "恢复一致": True}
    after = dict(live)
    applied = []
    for part, old, target in moves:
        if live.get(part) != old:
            restored = dict(after)
            for done_part, done_old, _ in reversed(applied):
                if done_old is None:
                    restored.pop(done_part, None)
                else:
                    restored[done_part] = done_old
            return {
                "凭证": key,
                "状态": "解除失败",
                "原因码": "STALE_HEAD",
                "失败分区": part,
                "现行": live.get(part),
                "冻结前": old,
                "已恢复": [
                    {"分区": done_part, "恢复版本": done_old}
                    for done_part, done_old, _ in reversed(applied)
                ],
                "恢复一致": restored == live,
                "说明": "指针已变动，已恢复冻结前旧版结果，可继续处理",
            }
        after[part] = target
        applied.append((part, old, target))
    restored = dict(after)
    for part, old, _ in reversed(applied):
        if old is None:
            restored.pop(part, None)
        else:
            restored[part] = old
    return {
        "凭证": key,
        "状态": "已解除",
        "复用": False,
        "解除": [
            {
                "分区": part,
                "旧版": old["version"] if old else None,
                "新版": target["version"],
            }
            for part, old, target in applied
        ],
        "恢复一致": restored == live,
        "结果摘要": fingerprint({part: after.get(part) for part in sorted(frozen)}),
    }


def backfill(store, key):
    document = store.get_credential(key)
    if document is None:
        raise ReplayError("CREDENTIAL", "凭证不存在")
    return {
        "凭证": key,
        "计划": document["凭证"]["计划"],
        "补数": [
            {"分区": entry["分区"], "当前": entry["当前"], "目标": entry["目标"], "依据": entry["依据"]}
            for entry in document["解除"]
        ],
        "暂缓": document["保持冻结"],
    }


def review(store, key):
    """回查同一份凭证；原异常证据缺失或被替换时已解除范围无法复核。"""
    document = store.get_credential(key)
    if document is None:
        raise ReplayError("CREDENTIAL", "凭证不存在")
    meta = document["凭证"]
    evidence = store.get_evidence(meta["证据编号"])
    released = document["解除"]
    if evidence is None:
        return {
            "凭证": key,
            "复核": "不可复核",
            "原因码": "EVIDENCE_GONE",
            "说明": "原异常证据已删除，已解除范围无法复核",
            "已解除": [entry["分区"] for entry in released],
        }
    if evidence_key(evidence) != meta["证据摘要"]:
        return {
            "凭证": key,
            "复核": "不可复核",
            "原因码": "EVIDENCE_CHANGED",
            "说明": "留存证据与凭证摘要不符，已解除范围无法复核",
            "已解除": [entry["分区"] for entry in released],
        }
    checks = []
    for entry in released:
        if entry["依据"] == "REPAIR_VERIFIED":
            ok = entry["修复校验值"] == evidence["expected"]
            note = "修复校验值与证据期望一致" if ok else "修复校验值与证据期望不符"
        else:
            inputs = {
                part: {"version": value["version"], "rows": as_int(value["rows"])}
                for part, value in entry["上游"].items()
            }
            name, date_text = entry["分区"].split("@")
            rebuilt = destination(name, entry["修订"], date_text, inputs)
            ok = rebuilt["version"] == entry["目标"]["version"]
            note = "重算版本与凭证一致" if ok else "重算版本与凭证不符"
        checks.append({"分区": entry["分区"], "依据": entry["依据"], "结论": "一致" if ok else "不符", "说明": note})
    passed = all(item["结论"] == "一致" for item in checks)
    return {
        "凭证": key,
        "复核": "通过" if passed else "未通过",
        "核对": checks,
        "保持冻结": document["保持冻结"],
        "证据": {
            "编号": meta["证据编号"],
            "检测器": evidence["detector"],
            "检测时间": evidence["detected"],
        },
    }
