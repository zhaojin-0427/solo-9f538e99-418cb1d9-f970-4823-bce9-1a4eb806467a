"""备用搭配复用、跨帖换配、持久化与旧批补建测试。"""
from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading
import time
import socket

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.core import Sheet, Signature, minimum_change_plan
from app.main import create_app


def _sig(no, front=None, back=None):
    return {
        "signature_no": no,
        "front_plate": front or f"{no}-F",
        "back_plate": back or f"{no}-B",
    }


def _sheet(no, plate=None):
    return {"sheet_no": no, "printed_plate": plate}


def _open_batch(client, signatures, sheets):
    resp = client.post("/batches", json={"signatures": signatures, "sheets": sheets})
    assert resp.status_code == 201, resp.text
    return resp.json()["batch_id"]


def _claim(client, batch_id, signature_no, sheet_no):
    return client.post(
        f"/batches/{batch_id}/claims",
        json={"signature_no": signature_no, "sheet_no": sheet_no},
    )


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "assignment-test.db"


@pytest.fixture()
def client(db_path):
    app = create_app(str(db_path))
    with TestClient(app) as c:
        yield c


def _detail(client, bid):
    resp = client.get(f"/batches/{bid}")
    assert resp.status_code == 200
    return resp.json()


def test_open_batch_persists_one_compatible_backup_sheet_per_signature(client, db_path):
    sigs = [_sig("S1", "A1", "X12"), _sig("S2", "X12", "X23"), _sig("S3", "X23", "A3")]
    sheets = [_sheet("P1", "A1"), _sheet("P2", "X12"), _sheet("P3", "X23"), _sheet("P4", "A3")]
    bid = _open_batch(client, sigs, sheets)

    detail = _detail(client, bid)
    assert detail["assignment_plan"] == {"S1": "P1", "S2": "P2", "S3": "P3"}
    for sig in detail["signatures"]:
        assert sig["assigned_sheet_no"] is not None

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT signature_no, sheet_no FROM assignments WHERE batch_id = ?", (bid,)
        ).fetchall()
        # 兼容视图也能读到同一套搭配。
        view_rows = conn.execute(
            "SELECT signature_no, sheet_no FROM plan_assignments WHERE batch_id = ?", (bid,)
        ).fetchall()
    assert dict(rows) == detail["assignment_plan"]
    assert dict(view_rows) == detail["assignment_plan"]


def test_claim_assigned_sheet_reuses_other_pairings_without_matching(
    client, monkeypatch
):
    bid = _open_batch(
        client,
        [_sig("S1", "A", "B"), _sig("S2", "C", "D"), _sig("S3", "E", "F")],
        [_sheet("P1"), _sheet("P2"), _sheet("P3"), _sheet("P4")],
    )
    plan = _detail(client, bid)["assignment_plan"]
    sig_no, sheet_no = next(iter(plan.items()))
    expected_remaining = {s: p for s, p in plan.items() if s != sig_no}

    def fail_match(*_args, **_kwargs):
        raise AssertionError("本领纸路径不应重新做余纸匹配")

    monkeypatch.setattr("app.main.feasible", fail_match)
    monkeypatch.setattr("app.main.build_plan", fail_match)
    monkeypatch.setattr("app.main.minimum_change_plan", fail_match)

    resp = _claim(client, bid, sig_no, sheet_no)
    assert resp.status_code == 200, resp.text
    assert resp.json()["replayed"] is False
    assert _detail(client, bid)["assignment_plan"] == expected_remaining

    again = _claim(client, bid, sig_no, sheet_no)
    assert again.status_code == 200
    assert again.json()["replayed"] is True
    assert _detail(client, bid)["assignment_plan"] == expected_remaining


def test_claim_unassigned_compatible_sheet_keeps_other_pairings(client, monkeypatch):
    bid = _open_batch(
        client,
        [_sig("S1", "A", "B"), _sig("S2", "C", "D")],
        [_sheet("P1"), _sheet("P2"), _sheet("P3")],
    )
    detail = _detail(client, bid)
    plan = detail["assignment_plan"]
    free_sheet = next(
        sheet["sheet_no"]
        for sheet in detail["sheets"]
        if sheet["claimed_by"] is None and sheet["assigned_signature_no"] is None
    )
    sig_no = next(iter(plan))
    expected_remaining = {s: p for s, p in plan.items() if s != sig_no}

    def fail_match(*_args, **_kwargs):
        raise AssertionError("领未配给任何帖的合用纸时应直接沿用其余搭配")

    monkeypatch.setattr("app.main.feasible", fail_match)
    monkeypatch.setattr("app.main.build_plan", fail_match)
    monkeypatch.setattr("app.main.minimum_change_plan", fail_match)

    resp = _claim(client, bid, sig_no, free_sheet)
    assert resp.status_code == 200, resp.text
    assert _detail(client, bid)["assignment_plan"] == expected_remaining


def test_cross_claim_replans_with_minimum_changed_unclaimed_signatures(client):
    # 合用边形成 P1-S1-P2-S2-P3-S3-P4 的链。S1 领走配给 S2 的 P2 后，
    # S2 必须改 P3、S3 必须改 P4：余帖仍可配齐，且两帖换纸是最少方案。
    sigs = [
        _sig("S1", "A1", "X12"),
        _sig("S2", "X12", "X23"),
        _sig("S3", "X23", "A3"),
    ]
    sheets = [_sheet("P1", "A1"), _sheet("P2", "X12"), _sheet("P3", "X23"), _sheet("P4", "A3")]
    bid = _open_batch(client, sigs, sheets)
    assert _detail(client, bid)["assignment_plan"] == {
        "S1": "P1",
        "S2": "P2",
        "S3": "P3",
    }

    resp = _claim(client, bid, "S1", "P2")
    assert resp.status_code == 200, resp.text
    assert resp.json()["sheet_no"] == "P2"

    detail = _detail(client, bid)
    assert detail["assignment_plan"] == {"S2": "P3", "S3": "P4"}
    instructions = client.get(f"/batches/{bid}/instructions").json()["instructions"]
    assert [(i["signature_no"], i["sheet_no"]) for i in instructions] == [("S1", "P2")]

    # 后续工人照新搭配领纸即可。
    assert _claim(client, bid, "S2", "P3").status_code == 200
    assert _claim(client, bid, "S3", "P4").status_code == 200
    assert _detail(client, bid)["complete"] is True


def test_minimum_change_planner_prefers_one_change_over_long_chain():
    sigs = [Signature("S1", "a", "b"), Signature("S2", "c", "d"), Signature("S3", "e", "f")]
    # P1/P2/P3 均为空白纸，P4 不合用；旧搭配为 S1-P1/S2-P2/S3-P3。
    sheets = [Sheet("P1", None), Sheet("P2", None), Sheet("P3", None), Sheet("P4", "NOPE")]
    old = {"S1": "P1", "S2": "P2", "S3": "P3"}
    # S1 已领走 P2 后，P2 退出；剩余可用纸为 S1 留下的 P1 与 P3。
    # S2 改 P1、S3 留 P3，只换一帖。
    remaining_sheets = [Sheet("P1", None), Sheet("P3", None)]
    plan = minimum_change_plan([s for s in sigs if s.signature_no != "S1"], remaining_sheets, old)
    assert plan == {"S2": "P1", "S3": "P3"}


def test_cross_claim_infeasible_keeps_instruction_and_plan_unchanged(client):
    bid = _open_batch(
        client,
        [_sig("S1", "A", "B"), _sig("S2", "C", "D")],
        [_sheet("P1", "A"), _sheet("P2")],
    )
    before = _detail(client, bid)["assignment_plan"]
    resp = _claim(client, bid, "S1", "P2")  # P2 原配 S2，领走后 S2 无纸
    assert resp.status_code == 409
    assert "余纸" in resp.json()["detail"]
    assert _detail(client, bid)["assignment_plan"] == before
    assert client.get(f"/batches/{bid}/instructions").json()["instructions"] == []

    assert _claim(client, bid, "S1", "P1").status_code == 200
    assert _claim(client, bid, "S2", "P2").status_code == 200


def test_assignment_plan_survives_restart(db_path):
    bid = None
    with TestClient(create_app(str(db_path))) as client:
        bid = _open_batch(
            client,
            [_sig("S1", "A", "B"), _sig("S2", "C", "D")],
            [_sheet("P1"), _sheet("P2"), _sheet("P3")],
        )
        plan = _detail(client, bid)["assignment_plan"]
        sig_no, sheet_no = next(iter(plan.items()))
        assert _claim(client, bid, sig_no, sheet_no).status_code == 200
        before = _detail(client, bid)["assignment_plan"]

    with TestClient(create_app(str(db_path))) as client:
        after = _detail(client, bid)["assignment_plan"]
        assert after == before
        sig_no, sheet_no = next(iter(after.items()))
        resp = _claim(client, bid, sig_no, sheet_no)
        assert resp.status_code == 200, resp.text


def test_legacy_batch_rebuilds_plan_from_remaining_signatures_and_sheets(db_path):
    legacy_schema = """
    CREATE TABLE batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE signatures (
        batch_id INTEGER NOT NULL, signature_no TEXT NOT NULL,
        front_plate TEXT NOT NULL, back_plate TEXT NOT NULL,
        PRIMARY KEY (batch_id, signature_no)
    );
    CREATE TABLE sheets (
        batch_id INTEGER NOT NULL, sheet_no TEXT NOT NULL, printed_plate TEXT,
        PRIMARY KEY (batch_id, sheet_no)
    );
    CREATE TABLE instructions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL, signature_no TEXT NOT NULL, sheet_no TEXT NOT NULL,
        prints TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (batch_id, signature_no), UNIQUE (batch_id, sheet_no)
    );
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute("INSERT INTO batches (id) VALUES (1)")
        conn.executemany(
            "INSERT INTO signatures VALUES (1, ?, ?, ?)",
            [("S1", "A", "B"), ("S2", "C", "D"), ("S3", "E", "F")],
        )
        conn.executemany(
            "INSERT INTO sheets VALUES (1, ?, ?)",
            [("P1", None), ("P2", None), ("P3", None), ("P4", None)],
        )
        conn.execute(
            "INSERT INTO instructions (batch_id, signature_no, sheet_no, prints) "
            "VALUES (1, 'S1', 'P1', ?)",
            (json.dumps([{"side": "正面", "plate": "A"}]),),
        )

    with TestClient(create_app(str(db_path))) as client:
        # 迁移会建新搭配表，但旧批在第一次领新单前没有搭配行。
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0

        resp = _claim(client, 1, "S2", "P2")
        assert resp.status_code == 200, resp.text
        plan = _detail(client, 1)["assignment_plan"]
        assert set(plan) == {"S3"}
        assert plan["S3"] in {"P3", "P4"}

        # 拒绝的旧批领纸不能提前保存补建结果。
        rejected = _claim(client, 1, "S3", "P1")  # P1 已被 S1 领用
        assert rejected.status_code == 409


def test_rejected_legacy_rebuild_is_not_persisted(db_path):
    legacy_schema = """
    CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT);
    CREATE TABLE signatures (batch_id INTEGER, signature_no TEXT, front_plate TEXT, back_plate TEXT,
                             PRIMARY KEY(batch_id, signature_no));
    CREATE TABLE sheets (batch_id INTEGER, sheet_no TEXT, printed_plate TEXT,
                         PRIMARY KEY(batch_id, sheet_no));
    CREATE TABLE instructions (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER,
                               signature_no TEXT, sheet_no TEXT, prints TEXT,
                               UNIQUE(batch_id, signature_no), UNIQUE(batch_id, sheet_no));
    """
    with sqlite3.connect(db_path) as conn:
        conn.executescript(legacy_schema)
        conn.execute("INSERT INTO batches (id) VALUES (1)")
        conn.executemany("INSERT INTO signatures VALUES (1, ?, ?, ?)",
                         [("S1", "A", "B"), ("S2", "C", "D")])
        conn.executemany("INSERT INTO sheets VALUES (1, ?, ?)",
                         [("P1", "A"), ("P2", None)])

    app = create_app(str(db_path))
    client = TestClient(app)
    resp = _claim(client, 1, "S1", "P2")  # 会抢走 S2 的唯一万能纸，余帖配不齐
    assert resp.status_code == 409
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM instructions").fetchone()[0] == 0


def test_assignment_save_failure_rolls_back_new_instruction(db_path, monkeypatch):
    app = create_app(str(db_path))
    with TestClient(app) as client:
        bid = _open_batch(client, [_sig("S1", "A", "B")], [_sheet("P1")])

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr("app.main._sync_assignments", fail_save)
    client = TestClient(app, raise_server_exceptions=False)
    resp = _claim(client, bid, "S1", "P1")
    assert resp.status_code == 500

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM instructions").fetchone()[0] == 0
        assert conn.execute("SELECT signature_no, sheet_no FROM assignments").fetchall() == [
            ("S1", "P1")
        ]


@pytest.fixture()
def live_server(db_path):
    app = create_app(str(db_path / "live.db"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(250):
        if server.started:
            break
        time.sleep(0.02)
    else:
        pytest.fail("服务未启动")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_concurrent_cross_claims_end_with_one_consistent_plan(live_server):
    """两帖并发热点抢同一纸时，先提交者生效；最终指令和搭配仍一一对应。"""
    with httpx.Client(base_url=live_server, timeout=10) as client:
        sigs = [_sig("S1", "A", "B"), _sig("S2", "A", "B"), _sig("S3", "C", "D")]
        sheets = [_sheet("P1"), _sheet("P2"), _sheet("P3")]
        bid = _open_batch(client, sigs, sheets)
        barrier = threading.Barrier(2)

        def claim(signature_no):
            barrier.wait()
            return _claim(client, bid, signature_no, "P1")

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(claim, ["S1", "S2"]))
        assert sorted(r.status_code for r in responses) == [200, 409]

        detail = _detail(client, bid)
        instructions = client.get(f"/batches/{bid}/instructions").json()["instructions"]
        assert len(instructions) == 1
        claimed_sig = instructions[0]["signature_no"]
        claimed_sheet = instructions[0]["sheet_no"]
        assert claimed_sheet == "P1"
        assert claimed_sig not in detail["assignment_plan"]
        assert "P1" not in detail["assignment_plan"].values()

        # 每个余帖都仍在当前搭配中有一张纸，且搭配纸均未被指令领用。
        claimed_sigs = {i["signature_no"] for i in instructions}
        assert set(detail["assignment_plan"]) == {"S1", "S2", "S3"} - claimed_sigs
        assert not (set(detail["assignment_plan"].values()) & {i["sheet_no"] for i in instructions})
