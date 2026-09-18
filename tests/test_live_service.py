"""Tests that run against a live, already-deployed service.

Used by the Docker Compose ``verify`` acceptance service, which sets
``API_BASE_URL=http://api:8000``.  All scenes and operation ids are unique per
run so the suite is safe to repeat against a persistent database.
"""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="API_BASE_URL not set; skipping live-service tests"
)


def _scene() -> str:
    return f"live-{uuid.uuid4().hex[:12]}"


def test_live_health():
    resp = httpx.get(f"{BASE_URL}/api/health", timeout=10.0)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_live_twenty_concurrent_operations_are_gapless():
    scene = _scene()
    op_ids = [uuid.uuid4().hex for _ in range(20)]

    with ThreadPoolExecutor(max_workers=20) as pool:
        responses = list(
            pool.map(
                lambda op_id: httpx.post(
                    f"{BASE_URL}/api/shot-numbers",
                    json={"scene_id": scene, "client_op_id": op_id, "notes": "并发"},
                    timeout=15.0,
                ),
                op_ids,
            )
        )

    assert all(r.status_code == 201 for r in responses)
    numbers = sorted(r.json()["shot_number"] for r in responses)
    assert numbers == list(range(1, 21))


def test_live_duplicate_and_conflict():
    scene = _scene()
    op_id = uuid.uuid4().hex
    payload = {"scene_id": scene, "client_op_id": op_id, "notes": "第一条"}

    first = httpx.post(f"{BASE_URL}/api/shot-numbers", json=payload, timeout=10.0)
    assert first.status_code == 201

    replay = httpx.post(f"{BASE_URL}/api/shot-numbers", json=payload, timeout=10.0)
    assert replay.status_code == 200
    assert replay.json()["shot_number"] == first.json()["shot_number"]
    assert replay.json()["replayed"] is True

    conflict = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={**payload, "notes": "换内容"},
        timeout=10.0,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "client_op_id_conflict"


def test_live_injected_failure_replays_original_number():
    scene = _scene()
    op_id = uuid.uuid4().hex
    payload = {"scene_id": scene, "client_op_id": op_id, "notes": "故障注入"}

    failed = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={**payload, "inject_failure_after_commit": True},
        timeout=10.0,
    )
    assert failed.status_code == 503

    retry = httpx.post(f"{BASE_URL}/api/shot-numbers", json=payload, timeout=10.0)
    assert retry.status_code == 200
    assert retry.json()["shot_number"] == 1
    assert retry.json()["replayed"] is True


def test_live_conflict_resolution_keeps_disjoint_remote_edit():
    """复现修复：409 后解决冲突的二次保存不得还原远端非冲突改动。

    两个终端从 r0 编辑同一备注；B 在一次保存里同时改与 A 重叠的第二行以及
    不重叠的第三行；A 收到 409 后按服务端脚手架真正重基，只整理第二行再保存。
    最终 r2 必须同时包含 A 整理后的第二行与 B 的第三行。
    """
    scene = _scene()
    issued = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={"scene_id": scene, "client_op_id": uuid.uuid4().hex,
              "notes": "第一行\n第二行\n第三行"},
        timeout=10.0,
    )
    assert issued.status_code == 201
    op_id = issued.json()["client_op_id"]

    def patch(base_revision: int, notes: str):
        return httpx.patch(
            f"{BASE_URL}/api/operations/{op_id}/notes",
            json={"client_op_id": op_id, "base_revision": base_revision,
                  "notes": notes},
            timeout=10.0,
        )

    # B 在一次保存里同时改第二、三行 -> r1。
    b = patch(0, "第一行\nB第二行\nB第三行")
    assert b.status_code == 200
    assert b.json()["notes_revision"] == 1

    # A 仍持 r0 全文，只改第二行 -> 409。
    clash = patch(0, "第一行\nA第二行\n第三行")
    assert clash.status_code == 409
    detail = clash.json()["detail"]
    assert detail["current"]["notes_revision"] == 1
    blocks = detail["merge_blocks"]
    assert blocks, "409 应携带有序合并脚手架"
    # 冲突仅限第二行，第三行是干净块里的远端非冲突改动。
    assert detail["conflicts"] == [
        {"base": "第二行\n", "mine": "A第二行\n", "theirs": "B第二行\n"}
    ]
    assert any(
        blk["type"] == "text" and "B第三行" in blk["text"] for blk in blocks
    )

    # 前端重基：干净块原样、冲突块先取本端文字；再仅整理冲突的第二行。
    draft = "".join(
        blk["text"] if blk["type"] == "text" else blk["mine"] for blk in blocks
    )
    resolved = draft.replace("A第二行", "A+B第二行")
    saved = patch(1, resolved)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["notes_revision"] == 2
    assert body["notes"] == "第一行\nA+B第二行\nB第三行"

    current = httpx.get(f"{BASE_URL}/api/operations/{op_id}", timeout=10.0)
    assert current.json()["notes"] == "第一行\nA+B第二行\nB第三行"


def test_live_note_revisions_merge_conflict_and_sequence():
    """备注可修订的完整现场流（对持久库安全，场景/标识每次唯一）。

    修订备注不影响幂等：同一 client_op_id 携发放时备注仍取回原号码；
    两终端不相交编辑自动合并，重叠编辑 409（数据库不动），解决后再存；
    整个过程镜号序列不受影响。
    """
    scene = _scene()
    op1 = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={"scene_id": scene, "client_op_id": uuid.uuid4().hex,
              "notes": "第一段\n第二段\n第三段\n"},
        timeout=10.0,
    )
    assert op1.status_code == 201
    op1_id = op1.json()["client_op_id"]
    assert op1.json()["notes_revision"] == 0

    def patch(base_revision: int, notes: str):
        return httpx.patch(
            f"{BASE_URL}/api/operations/{op1_id}/notes",
            json={"client_op_id": op1_id, "base_revision": base_revision,
                  "notes": notes},
            timeout=10.0,
        )

    # 迁移/重放不变式：发放备注是不可变指纹。修订后用原始备注重试发放，
    # 仍然幂等取回原镜号。
    saved = patch(0, "第一段\n第二段\n第三段（补充）\n")
    assert saved.status_code == 200
    assert saved.json()["notes_revision"] == 1

    replay = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={"scene_id": scene, "client_op_id": op1_id,
              "notes": "第一段\n第二段\n第三段\n"},
        timeout=10.0,
    )
    assert replay.status_code == 200
    assert replay.json()["shot_number"] == 1
    assert replay.json()["replayed"] is True

    # 两终端：终端 A 从旧基础号 r0 改第一段；服务端当前为 r1（改了第三段），
    # 两处不相交 -> 自动合并，只产生一个新修订 r2。
    merged = patch(0, "第一段（A改）\n第二段\n第三段\n")
    assert merged.status_code == 200
    body = merged.json()
    assert body["merge_status"] == "merged"
    assert body["notes_revision"] == 2
    assert body["notes"] == "第一段（A改）\n第二段\n第三段（补充）\n"

    # 重叠改动 -> 409 且带三方片段，数据库保持在 r2。
    clash = patch(0, "完全重写的内容，与任何版本都不相交但替换了全文")
    assert clash.status_code == 409
    detail = clash.json()["detail"]
    assert detail["error"] == "notes_conflict"
    assert detail["current"]["notes_revision"] == 2
    assert len(detail["conflicts"]) >= 1

    current = httpx.get(f"{BASE_URL}/api/operations/{op1_id}", timeout=10.0)
    assert current.json()["notes_revision"] == 2

    # 场记以服务端当前版本为基础整理后再次保存 -> r3。
    resolved = patch(2, body["notes"] + "整理后的结尾\n")
    assert resolved.status_code == 200
    assert resolved.json()["notes_revision"] == 3

    # 历史版本完整。
    history = httpx.get(
        f"{BASE_URL}/api/operations/{op1_id}/note-revisions", timeout=10.0
    )
    assert history.status_code == 200
    assert [r["revision"] for r in history.json()] == [0, 1, 2, 3]

    # 镜号序列不受备注修订影响：下一条新镜号为 #2。
    op2 = httpx.post(
        f"{BASE_URL}/api/shot-numbers",
        json={"scene_id": scene, "client_op_id": uuid.uuid4().hex, "notes": "第二条"},
        timeout=10.0,
    )
    assert op2.status_code == 201
    assert op2.json()["shot_number"] == 2

    listing = httpx.get(f"{BASE_URL}/api/scenes/{scene}/operations", timeout=10.0)
    assert [op["shot_number"] for op in listing.json()] == [1, 2]
    assert listing.json()[0]["notes_revision"] == 3
