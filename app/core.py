"""补印核心规则：纸张合用判定、二分图匹配与最小换帖重配。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signature:
    """书帖：一帖有正、反两个版号。"""

    signature_no: str
    front_plate: str
    back_plate: str


@dataclass(frozen=True)
class Sheet:
    """纸张：printed_plate 为 None 表示空白纸。"""

    sheet_no: str
    printed_plate: str | None


def prints_for(sig: Signature, sheet: Sheet) -> list[dict[str, str]] | None:
    """判断纸张对该帖是否合用；合用则返回待印面与版号清单，否则返回 None。

    - 空白纸：正、反两面都印；
    - 已印版号对上正面：补印反面；
    - 已印版号对上反面：补印正面；
    - 对不上任何一面：不合用。
    """
    if sheet.printed_plate is None:
        return [
            {"side": "正面", "plate": sig.front_plate},
            {"side": "反面", "plate": sig.back_plate},
        ]
    if sheet.printed_plate == sig.front_plate:
        return [{"side": "反面", "plate": sig.back_plate}]
    if sheet.printed_plate == sig.back_plate:
        return [{"side": "正面", "plate": sig.front_plate}]
    return None


def adjacency(
    signatures: list[Signature], sheets: list[Sheet]
) -> dict[str, list[str]]:
    """构造 {帖号: [合用纸号]}，纸号顺序跟随入参。"""
    return {
        sig.signature_no: [
            sheet.sheet_no for sheet in sheets if prints_for(sig, sheet) is not None
        ]
        for sig in signatures
    }


def build_plan(signatures: list[Signature], sheets: list[Sheet]) -> dict[str, str]:
    """求一套尽量覆盖全部书帖的备用搭配，返回 {帖号: 纸号}。

    使用增广路算法；未被覆盖的帖不会出现在返回值中。调用方可用长度判断是否
    全册配得齐。
    """
    graph = adjacency(signatures, sheets)
    sheet_owner: dict[str, str] = {}  # 纸号 -> 帖号

    def augment(signature_no: str, seen: set[str]) -> bool:
        for sheet_no in graph[signature_no]:
            if sheet_no in seen:
                continue
            seen.add(sheet_no)
            owner = sheet_owner.get(sheet_no)
            if owner is None or augment(owner, seen):
                sheet_owner[sheet_no] = signature_no
                return True
        return False

    for sig in signatures:
        augment(sig.signature_no, set())

    return {sig_no: sheet_no for sheet_no, sig_no in sheet_owner.items()}


def max_matching(signatures: list[Signature], sheets: list[Sheet]) -> dict[str, str]:
    """书帖—纸张二分图最大匹配（增广路法）。

    为兼容既有调用，返回 ``{纸号: 帖号}``。
    """
    plan = build_plan(signatures, sheets)
    return {sheet_no: sig_no for sig_no, sheet_no in plan.items()}


def feasible(signatures: list[Signature], sheets: list[Sheet]) -> bool:
    """每帖都能各配一张合用纸张（全册配得齐）时为 True。"""
    return len(build_plan(signatures, sheets)) == len(signatures)


def minimum_change_plan(
    signatures: list[Signature],
    sheets: list[Sheet],
    previous: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """在所有完美搭配中，选相对 ``previous`` 换纸帖数最少的一套。

    返回 ``{帖号: 纸号}``；若无法覆盖全部书帖则返回 None。

    规模上限为 24 帖 / 48 纸，这里用带权二分图最小费用流求全局最优。一条
    “换纸”边的主费用为 1；另加很小的纸号顺序费用，仅用于在相同换纸数下
    得到稳定、可复现的选择。
    """
    previous = previous or {}
    if not signatures:
        return {}

    sheet_index = {sheet.sheet_no: i for i, sheet in enumerate(sheets)}
    edges_by_sig = adjacency(signatures, sheets)

    sig_count = len(signatures)
    sheet_count = len(sheets)
    source = 0
    sig_start = 1
    sheet_start = sig_start + sig_count
    sink = sheet_start + sheet_count
    node_count = sink + 1

    graph: list[list[list[int]]] = [[] for _ in range(node_count)]

    def add_edge(u: int, v: int, capacity: int, cost: int) -> list[int]:
        edge = [v, capacity, cost, len(graph[v])]
        reverse = [u, 0, -cost, len(graph[u])]
        graph[u].append(edge)
        graph[v].append(reverse)
        return edge

    choice_edges: dict[tuple[int, int], list[int]] = {}
    for j in range(sheet_count):
        add_edge(sheet_start + j, sink, 1, 0)

    for i, sig in enumerate(signatures):
        sig_node = sig_start + i
        add_edge(source, sig_node, 1, 0)
        compatible = set(edges_by_sig[sig.signature_no])
        for j, sheet in enumerate(sheets):
            if sheet.sheet_no not in compatible:
                continue
            changed = 0 if previous.get(sig.signature_no) == sheet.sheet_no else 1
            # sheet_count <= 48；24 帖的顺序费用总和远小于一次“换纸”的主费用。
            cost = changed * 10_000 + j
            choice_edges[(i, j)] = add_edge(
                sig_node, sheet_start + j, 1, cost
            )

    flow = 0
    while flow < sig_count:
        infinity = 10**18
        dist = [infinity] * node_count
        parent_node = [-1] * node_count
        parent_edge = [-1] * node_count
        in_queue = [False] * node_count
        dist[source] = 0

        # SPFA：残量网络中有负费用反向边；本问题规模很小。
        queue = [source]
        in_queue[source] = True
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            in_queue[u] = False
            for edge_index, edge in enumerate(graph[u]):
                v, capacity, cost, _rev = edge
                if capacity <= 0 or dist[v] <= dist[u] + cost:
                    continue
                dist[v] = dist[u] + cost
                parent_node[v] = u
                parent_edge[v] = edge_index
                if not in_queue[v]:
                    queue.append(v)
                    in_queue[v] = True

        if dist[sink] == infinity:
            return None

        v = sink
        while v != source:
            u = parent_node[v]
            edge_index = parent_edge[v]
            if u < 0 or edge_index < 0:
                return None
            edge = graph[u][edge_index]
            edge[1] -= 1
            reverse_index = edge[3]
            graph[v][reverse_index][1] += 1
            v = u
        flow += 1

    plan: dict[str, str] = {}
    for (i, j), edge in choice_edges.items():
        # 初始容量为 1；最终容量为 0 表示该边被选中。
        if edge[1] == 0:
            plan[signatures[i].signature_no] = sheets[j].sheet_no

    if len(plan) != sig_count:
        return None
    return plan


def changed_signature_count(
    old_plan: dict[str, str], new_plan: dict[str, str]
) -> int:
    """两套搭配之间，配纸发生变化的帖数。"""
    return sum(1 for sig_no, sheet_no in new_plan.items() if old_plan.get(sig_no) != sheet_no)
