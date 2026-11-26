"""样书补印 API。

印厂用手头的单面印张和少量空白纸配齐一册样书：

- 开批：登记至多 24 帖的正反面版号、至多 48 张纸的纸号与已印版号（空白纸留空），
  全册配得齐才开批；同时保存一套每帖一张合用纸的备用搭配；
- 领纸：工人为书帖报纸号。优先复用备用搭配：
  * 领本帖配纸，或领一张未领用、也未配给任何帖的合用纸：其余搭配不动；
  * 领走配给别帖的纸：在同一事务内发指令并把余帖重配为换纸帖数最少的方案；
- 同批并发领纸由 SQLite 写锁串行化；同帖同纸重试返回原指令；
- 搭配和指令一起提交；拒绝或保存失败一起回滚；
- 搭配存 SQLite，重启后继续复用；没有搭配记录的旧批在第一次领新单时补建。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException

from . import db
from .core import (
    Sheet,
    Signature,
    build_plan,
    feasible,
    minimum_change_plan,
    prints_for,
)
from .schemas import BatchCreate, BatchOut, ClaimIn, InstructionOut, PrintJob

DEFAULT_DB_PATH = os.environ.get("REPRINT_DB", "reprint.db")


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 事务已提交或已回滚


def _instruction_from_row(row: sqlite3.Row, replayed: bool) -> InstructionOut:
    return InstructionOut(
        batch_id=row["batch_id"],
        signature_no=row["signature_no"],
        sheet_no=row["sheet_no"],
        prints=[PrintJob(**p) for p in json.loads(row["prints"])],
        replayed=replayed,
    )


def _signature_from_row(row: sqlite3.Row) -> Signature:
    return Signature(row["signature_no"], row["front_plate"], row["back_plate"])


def _sheet_from_row(row: sqlite3.Row) -> Sheet:
    return Sheet(row["sheet_no"], row["printed_plate"])


def _replace_assignments(
    conn: sqlite3.Connection, batch_id: int, plan: dict[str, str]
) -> None:
    """整批替换备用搭配（用于开批和旧批补建后的落库）。"""
    conn.execute("DELETE FROM assignments WHERE batch_id = ?", (batch_id,))
    conn.executemany(
        """
        INSERT INTO assignments (batch_id, signature_no, sheet_no)
        VALUES (?, ?, ?)
        """,
        [(batch_id, sig_no, sheet_no) for sig_no, sheet_no in sorted(plan.items())],
    )


def _sync_assignments(
    conn: sqlite3.Connection,
    batch_id: int,
    old_plan: dict[str, str],
    new_plan: dict[str, str],
) -> None:
    """按最小改动更新搭配表：未变化的行保持不动。

    跨帖换配可能形成交换/交替链，因此先删除所有变化帖的旧行，再插入新行，
    避免两张纸的 UNIQUE 搭配在逐条 UPDATE 时瞬时冲突。
    """
    for sig_no in old_plan:
        if sig_no not in new_plan:
            conn.execute(
                "DELETE FROM assignments WHERE batch_id = ? AND signature_no = ?",
                (batch_id, sig_no),
            )

    changed = [
        sig_no
        for sig_no, sheet_no in new_plan.items()
        if old_plan.get(sig_no) != sheet_no
    ]
    if not changed:
        return

    conn.executemany(
        "DELETE FROM assignments WHERE batch_id = ? AND signature_no = ?",
        [(batch_id, sig_no) for sig_no in changed],
    )
    conn.executemany(
        """
        INSERT INTO assignments (batch_id, signature_no, sheet_no)
        VALUES (?, ?, ?)
        """,
        [(batch_id, sig_no, new_plan[sig_no]) for sig_no in changed],
    )


def _claim_locked(
    conn: sqlite3.Connection, batch_id: int, signature_no: str, sheet_no: str
) -> InstructionOut:
    """在已持有写锁（BEGIN IMMEDIATE）的连接上处理一次领纸。"""
    if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
        raise HTTPException(404, f"批 {batch_id} 不存在")
    sig_row = conn.execute(
        "SELECT * FROM signatures WHERE batch_id = ? AND signature_no = ?",
        (batch_id, signature_no),
    ).fetchone()
    if sig_row is None:
        raise HTTPException(404, f"批 {batch_id} 中没有帖 {signature_no}")
    sheet_row = conn.execute(
        "SELECT * FROM sheets WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if sheet_row is None:
        raise HTTPException(404, f"批 {batch_id} 中没有纸 {sheet_no}")

    # 同帖同纸重试：返回原指令；同帖换纸：一帖只发一张，拒发
    existing = conn.execute(
        "SELECT * FROM instructions WHERE batch_id = ? AND signature_no = ?",
        (batch_id, signature_no),
    ).fetchone()
    if existing is not None:
        if existing["sheet_no"] == sheet_no:
            return _instruction_from_row(existing, replayed=True)
        raise HTTPException(409, f"帖 {signature_no} 已领纸 {existing['sheet_no']}，一帖只发一张")

    # 一张纸只发给一帖
    taken = conn.execute(
        "SELECT signature_no FROM instructions WHERE batch_id = ? AND sheet_no = ?",
        (batch_id, sheet_no),
    ).fetchone()
    if taken is not None:
        raise HTTPException(409, f"纸 {sheet_no} 已发给帖 {taken['signature_no']}，一张纸只发给一帖")

    # 纸张合用：已印版号对上哪一面就补印另一面，空白纸两面都印
    sig = _signature_from_row(sig_row)
    sheet = _sheet_from_row(sheet_row)
    prints = prints_for(sig, sheet)
    if prints is None:
        raise HTTPException(
            409,
            f"纸 {sheet_no} 的已印版号「{sheet_row['printed_plate']}」与帖 {signature_no} "
            "的正反面版号都对不上，纸张不合用",
        )

    sig_rows = conn.execute(
        "SELECT * FROM signatures WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    sheet_rows = conn.execute(
        "SELECT * FROM sheets WHERE batch_id = ?", (batch_id,)
    ).fetchall()
    claimed_rows = conn.execute(
        "SELECT signature_no, sheet_no FROM instructions WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    claimed_sig_nos = {row["signature_no"] for row in claimed_rows}
    claimed_sheet_nos = {row["sheet_no"] for row in claimed_rows}

    unclaimed_sig_rows = [
        row for row in sig_rows if row["signature_no"] not in claimed_sig_nos
    ]
    available_sheet_rows = [
        row for row in sheet_rows if row["sheet_no"] not in claimed_sheet_nos
    ]
    unclaimed_sig_nos = {row["signature_no"] for row in unclaimed_sig_rows}
    available_sheet_nos = {row["sheet_no"] for row in available_sheet_rows}

    stored_rows = conn.execute(
        "SELECT signature_no, sheet_no FROM assignments WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()
    stored_plan = {row["signature_no"]: row["sheet_no"] for row in stored_rows}

    # 新批一定有搭配。旧批没有 assignments 记录，或状态异常不完整时，按当前
    # 余帖和未领纸补建一套；补建结果只在本次领纸事务提交时才真正生效。
    plan_was_rebuilt = (
        set(stored_plan) != unclaimed_sig_nos
        or any(paper not in available_sheet_nos for paper in stored_plan.values())
    )
    if plan_was_rebuilt:
        rebuilt = build_plan(
            [_signature_from_row(row) for row in unclaimed_sig_rows],
            [_sheet_from_row(row) for row in available_sheet_rows],
        )
        if len(rebuilt) != len(unclaimed_sig_rows):
            raise HTTPException(
                409, "余纸不够给未领的书帖各配一张，不予发放"
            )
        old_plan = rebuilt
    else:
        old_plan = stored_plan

    assigned_paper = old_plan.get(signature_no)
    assigned_owner = next(
        (sig_no for sig_no, paper in old_plan.items() if paper == sheet_no),
        None,
    )

    if assigned_paper == sheet_no:
        # 领的是配给本帖的纸：删掉本帖搭配即可，其余行原样保留。
        new_plan = {
            sig_no: paper
            for sig_no, paper in old_plan.items()
            if sig_no != signature_no
        }
    elif assigned_owner is None:
        # 该合用纸尚未领用，也没有配给任何余帖：本帖原配纸释放，其余搭配不动。
        new_plan = {
            sig_no: paper
            for sig_no, paper in old_plan.items()
            if sig_no != signature_no
        }
    else:
        # 领走了配给别帖的合用纸。当前帖和被领纸都退出，给剩余余帖重配，
        # 并在所有可行方案中选未领帖换纸数最少的一套。
        remaining_sigs = [
            _signature_from_row(row)
            for row in unclaimed_sig_rows
            if row["signature_no"] != signature_no
        ]
        remaining_sheets = [
            _sheet_from_row(row)
            for row in available_sheet_rows
            if row["sheet_no"] != sheet_no
        ]
        if not feasible(remaining_sigs, remaining_sheets):
            raise HTTPException(
                409,
                f"纸 {sheet_no} 已配给帖 {assigned_owner}；领走此纸后，"
                "余纸不够给其余未领书帖各配一张，不予发放",
            )
        replanned = minimum_change_plan(remaining_sigs, remaining_sheets, old_plan)
        if replanned is None:
            raise HTTPException(
                409,
                f"纸 {sheet_no} 已配给帖 {assigned_owner}；领走此纸后，"
                "余纸不够给其余未领书帖各配一张，不予发放",
            )
        new_plan = replanned

    cur = conn.execute(
        "INSERT INTO instructions (batch_id, signature_no, sheet_no, prints) VALUES (?, ?, ?, ?)",
        (batch_id, signature_no, sheet_no, json.dumps(prints, ensure_ascii=False)),
    )

    if plan_was_rebuilt:
        # 旧批补建：直接保存本次领纸后的完整余帖搭配。
        _replace_assignments(conn, batch_id, new_plan)
    else:
        _sync_assignments(conn, batch_id, stored_plan, new_plan)

    row = conn.execute("SELECT * FROM instructions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _instruction_from_row(row, replayed=False)


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or DEFAULT_DB_PATH
    db.init_db(path)

    app = FastAPI(
        title="样书补印 API",
        version="1.1.0",
        description="单面印张加空白纸配齐一册样书：开批时预存搭配，领纸时复用并最小换配重配。",
    )

    @contextmanager
    def session():
        """在端点所在线程内创建并关闭连接（sqlite3 连接不能跨线程使用）。"""
        conn = db.connect(path)
        try:
            yield conn
        finally:
            conn.close()

    @app.post("/batches", response_model=BatchOut, status_code=201, summary="开批")
    def create_batch(payload: BatchCreate):
        """登记书帖与纸张并开批。全册配得齐才开批，否则返回 409 且不落库。"""
        sig_nos = [s.signature_no for s in payload.signatures]
        sheet_nos = [s.sheet_no for s in payload.sheets]
        if len(set(sig_nos)) != len(sig_nos):
            raise HTTPException(422, "帖号重复")
        if len(set(sheet_nos)) != len(sheet_nos):
            raise HTTPException(422, "纸号重复")

        signatures = [
            Signature(s.signature_no, s.front_plate, s.back_plate) for s in payload.signatures
        ]
        sheets = [Sheet(s.sheet_no, s.printed_plate) for s in payload.sheets]
        if not feasible(signatures, sheets):
            raise HTTPException(409, "全册配不齐：现有纸张无法给每帖各配一张合用纸张，不予开批")
        plan = build_plan(signatures, sheets)

        with session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                batch_id = conn.execute("INSERT INTO batches DEFAULT VALUES").lastrowid
                conn.executemany(
                    "INSERT INTO signatures (batch_id, signature_no, front_plate, back_plate) "
                    "VALUES (?, ?, ?, ?)",
                    [(batch_id, s.signature_no, s.front_plate, s.back_plate) for s in signatures],
                )
                conn.executemany(
                    "INSERT INTO sheets (batch_id, sheet_no, printed_plate) VALUES (?, ?, ?)",
                    [(batch_id, s.sheet_no, s.printed_plate) for s in sheets],
                )
                _replace_assignments(conn, batch_id, plan)
                conn.execute("COMMIT")
            except Exception:
                _rollback(conn)
                raise
            created_at = conn.execute(
                "SELECT created_at FROM batches WHERE id = ?", (batch_id,)
            ).fetchone()["created_at"]
        return BatchOut(
            batch_id=batch_id,
            created_at=created_at,
            signature_count=len(signatures),
            sheet_count=len(sheets),
        )

    def _batch_detail(conn: sqlite3.Connection, batch_id: int) -> dict:
        batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if batch is None:
            raise HTTPException(404, f"批 {batch_id} 不存在")
        sigs = conn.execute(
            """
            SELECT s.signature_no, s.front_plate, s.back_plate,
                   i.sheet_no AS claimed_sheet,
                   a.sheet_no AS assigned_sheet_no
            FROM signatures s
            LEFT JOIN instructions i
              ON i.batch_id = s.batch_id AND i.signature_no = s.signature_no
            LEFT JOIN assignments a
              ON a.batch_id = s.batch_id AND a.signature_no = s.signature_no
            WHERE s.batch_id = ?
            ORDER BY s.signature_no
            """,
            (batch_id,),
        ).fetchall()
        sheets = conn.execute(
            """
            SELECT sh.sheet_no, sh.printed_plate,
                   i.signature_no AS claimed_by,
                   a.signature_no AS assigned_signature_no
            FROM sheets sh
            LEFT JOIN instructions i
              ON i.batch_id = sh.batch_id AND i.sheet_no = sh.sheet_no
            LEFT JOIN assignments a
              ON a.batch_id = sh.batch_id AND a.sheet_no = sh.sheet_no
            WHERE sh.batch_id = ?
            ORDER BY sh.sheet_no
            """,
            (batch_id,),
        ).fetchall()

        signature_items = []
        for row in sigs:
            item = dict(row)
            paper = item["assigned_sheet_no"]
            item["planned_sheet_no"] = paper
            item["backup_sheet_no"] = paper
            item["matched_sheet_no"] = paper
            item["assigned_sheet"] = paper
            item["planned_sheet"] = paper
            item["matched_sheet"] = paper
            signature_items.append(item)

        sheet_items = []
        for row in sheets:
            item = dict(row)
            owner = item["assigned_signature_no"]
            item["planned_for_signature_no"] = owner
            item["matched_signature_no"] = owner
            item["assigned_to"] = owner
            item["planned_for"] = owner
            sheet_items.append(item)

        plan_rows = conn.execute(
            """
            SELECT signature_no, sheet_no
            FROM assignments
            WHERE batch_id = ?
            ORDER BY signature_no
            """,
            (batch_id,),
        ).fetchall()
        assignment_plan = {row["signature_no"]: row["sheet_no"] for row in plan_rows}

        return {
            "batch_id": batch_id,
            "created_at": batch["created_at"],
            "complete": all(row["claimed_sheet"] for row in sigs),
            "signatures": signature_items,
            "sheets": sheet_items,
            "assignment_plan": assignment_plan,
            "assignments": [dict(row) for row in plan_rows],
            # Common descriptive aliases used by ops scripts / earlier drafts.
            "plan": assignment_plan,
            "matching": assignment_plan,
            "backup_plan": assignment_plan,
            "planned_assignments": [dict(row) for row in plan_rows],
        }

    @app.get("/batches/{batch_id}", summary="批次详情")
    def get_batch(batch_id: int):
        with session() as conn:
            return _batch_detail(conn, batch_id)

    @app.get("/batches/{batch_id}/assignments", summary="查询当前备用搭配")
    @app.get("/batches/{batch_id}/assignment", include_in_schema=False)
    @app.get("/batches/{batch_id}/plan", summary="查询当前备用搭配（plan 别名）")
    @app.get("/batches/{batch_id}/matching", include_in_schema=False)
    def get_assignments(batch_id: int):
        with session() as conn:
            detail = _batch_detail(conn, batch_id)
        return {
            "batch_id": detail["batch_id"],
            "assignment_plan": detail["assignment_plan"],
            "assignments": detail["assignments"],
            "plan": detail["plan"],
            "matching": detail["matching"],
            "backup_plan": detail["backup_plan"],
            "planned_assignments": detail["planned_assignments"],
        }

    @app.post(
        "/batches/{batch_id}/claims", response_model=InstructionOut, summary="领纸（发补印指令）"
    )
    def claim(batch_id: int, payload: ClaimIn):
        """工人为书帖报纸号领纸。规则见模块 docstring；失败不占纸、不改搭配。"""
        with session() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = _claim_locked(conn, batch_id, payload.signature_no, payload.sheet_no)
                conn.execute("COMMIT")
                return result
            except Exception:
                _rollback(conn)
                raise

    @app.get("/batches/{batch_id}/instructions", summary="按批查询补印指令")
    def list_instructions(batch_id: int):
        with session() as conn:
            if conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone() is None:
                raise HTTPException(404, f"批 {batch_id} 不存在")
            rows = conn.execute(
                "SELECT * FROM instructions WHERE batch_id = ? ORDER BY id", (batch_id,)
            ).fetchall()
        return {
            "batch_id": batch_id,
            "instructions": [
                {
                    "signature_no": r["signature_no"],
                    "sheet_no": r["sheet_no"],
                    "prints": json.loads(r["prints"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    return app


app = create_app()
