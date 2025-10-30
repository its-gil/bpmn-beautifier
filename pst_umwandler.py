import xml.etree.ElementTree as ET
from collections import defaultdict, deque
 
# ein PST Knoten
class Node:
    def __init__(self, ntype, value=None, children=None):
        self.ntype = ntype   # "SEQ", "XOR", "AND", "TASK", "EVENT", "NULL", "LOOPBACK"
        self.value = value
        self.children = children or []
 
    def __repr__(self, level=0):
        indent = "  " * level
        if self.ntype in ("TASK", "EVENT", "NULL", "LOOPBACK"):
            return f"{indent}{self.ntype}({self.value})"
        else:
            s = f"{indent}{self.ntype}(\n"
            for c in self.children:
                s += c.__repr__(level + 1) + ",\n"
            s += indent + ")"
            return s
 
# parsing
def parse_bpmn(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    ns = {"bpmn2": "http://www.omg.org/spec/BPMN/20100524/MODEL"}
 
    # tasks
    task_tags = [
        "task","userTask","manualTask","scriptTask","serviceTask",
        "businessRuleTask","sendTask","receiveTask","callActivity","subProcess"
    ]
    tasks = {}
    for tag in task_tags:
        for t in root.findall(f".//bpmn2:{tag}", ns):
            tasks[t.attrib["id"]] = t
 
    # events
    start_events = {e.attrib["id"]: e for e in root.findall(".//bpmn2:startEvent", ns)}
    end_events   = {e.attrib["id"]: e for e in root.findall(".//bpmn2:endEvent", ns)}
    events = {**start_events, **end_events}
 
    # gateways
    xor_gws = {g.attrib["id"]: g for g in root.findall(".//bpmn2:exclusiveGateway", ns)}
    and_gws = {g.attrib["id"]: g for g in root.findall(".//bpmn2:parallelGateway", ns)}
    gateways = {**xor_gws, **and_gws}
 
    # sequenceFlows
    flows = []
    for f in root.findall(".//bpmn2:sequenceFlow", ns):
        flows.append((f.attrib["sourceRef"], f.attrib["targetRef"]))
 
    # Graph
    succ = defaultdict(list)
    pred = defaultdict(list)
    for u, v in flows:
        succ[u].append(v)
        pred[v].append(u)
 
    # Helpers
    def is_xor(gw_id): return gw_id in xor_gws
    def is_and(gw_id): return gw_id in and_gws
    def outdeg(n): return len(succ.get(n, []))
    def indeg(n): return len(pred.get(n, []))
 
    def label(elem):
        if elem is None: return "NULL"
        nm = elem.attrib.get("name")
        if nm: return f"{elem.attrib['id']}|{nm.strip()}"
        return elem.attrib["id"]
 
    def label_by_id(nid):
        if nid in tasks: return label(tasks[nid])
        if nid in events: return label(events[nid])
        if nid in gateways: return label(gateways[nid])
        return nid
 
    # start event auswaehlen (no incoming if possible)
    if start_events:
        se = None
        for sid, _ in start_events.items():
            if indeg(sid) == 0:
                se = sid
                break
        start_id = se if se else next(iter(start_events.keys()))
    else:
        # Fallback: any node with indeg 0
        candidates = [n for n in set(tasks)|set(events)|set(gateways) if indeg(n)==0]
        start_id = candidates[0] if candidates else next(iter((tasks or events or gateways)))
 
    # ---------- reachability + join detection (for structured pairs) ----------
    def bfs_dist(start, stop=None):
        q = deque([start])
        visited = {start}
        dist = {start: 0}
        while q:
            u = q.popleft()
            if stop is not None and u == stop:
                return visited, dist
            for v in succ.get(u, []):
                if v not in visited:
                    visited.add(v)
                    dist[v] = dist[u] + 1
                    q.append(v)
        return visited, dist
 
    def find_join(split_id, kind):
        outs = succ.get(split_id, [])
        if len(outs) <= 1:
            return None
        visited_sets = []
        dist_maps = []
        for o in outs:
            vis, dmap = bfs_dist(o)
            visited_sets.append(vis)
            dist_maps.append(dmap)
        common = set.intersection(*visited_sets) if visited_sets else set()
 
        def is_candidate(n):
            return (n in gateways and ((kind == "XOR" and is_xor(n)) or (kind == "AND" and is_and(n))) and indeg(n) > 1)
 
        cands = [n for n in common if is_candidate(n)]
        if not cands:
            return None
 
        def score(n):
            return max(dm.get(n, 10**9) for dm in dist_maps)
 
        cands.sort(key=score)
        return cands[0]
 
    def edge_hits_active_split(u, active_split_stack):
        """Return split_id if any successor of u points to a split currently on the stack."""
        if not active_split_stack:
            return None
        active_set = set(active_split_stack)
        for v in succ.get(u, []):
            if v in active_set:
                return v
        return None
 
    def build_path_until(node_id, stop_id, active_split_stack):
        """
        Build subtree for a single branch from node_id until:
        - stop_id (exclusive)
        - we hit a LOOPBACK to some split in active_split_stack (→ LOOP node)
        """
        seq_children = []
        current = node_id
        seen_local = set()
 
        while current is not None and current != stop_id and current not in seen_local:
            seen_local.add(current)
 
            # --- 1) Check for loopback edge ---
            hit = edge_hits_active_split(current, active_split_stack)
            if hit:
                # Condition part = what we have so far
                condition_branch = (
                    Node("SEQ", None, seq_children) if len(seq_children) > 1
                    else (seq_children[0] if seq_children else Node("NULL"))
                )
 
                # Prevent infinite recursion: if we already are at the split, stop here
                if node_id == hit:
                    loop_body = Node("NULL")
                else:
                    # Build only *one pass* of the loop body, not re-expanding the same split infinitely
                    loop_body = Node("SEQ", None, [
                        Node("TASK", f"Body_of_{label_by_id(hit)}")
                    ])
 
                return Node("LOOP", None, [condition_branch, loop_body])
 
            # --- 2) Normal node handling ---
            if current in tasks:
                seq_children.append(Node("TASK", label(tasks[current])))
                nxts = succ.get(current, [])
                current = nxts[0] if len(nxts) == 1 else None
                continue
 
            if current in events:
                seq_children.append(Node("EVENT", label(events[current])))
                nxts = succ.get(current, [])
                current = nxts[0] if len(nxts) == 1 else None
                continue
 
            if current in gateways:
                # Handle splits (XOR / AND)
                if outdeg(current) > 1:
                    kind = "XOR" if is_xor(current) else ("AND" if is_and(current) else "XOR")
                    join = find_join(current, kind)
 
                    # prevent self-recursion: if join == current, break
                    if join == current:
                        break
 
                    active_split_stack.append(current)
                    kids = []
                    for o in succ[current]:
                        # avoid self-loop recursion
                        if o == current:
                            kids.append(Node("NULL"))
                            continue
                        kids.append(build_path_until(o, join, active_split_stack.copy()))
                    seq_children.append(Node(kind, label(gateways[current]), kids))
                    active_split_stack.pop()
 
                    nxts_after_join = succ.get(join, [])
                    current = nxts_after_join[0] if nxts_after_join else None
                    continue
                else:
                    nxts = succ.get(current, [])
                    current = nxts[0] if len(nxts) == 1 else None
                    continue
 
            break  # fallback
 
        # --- 3) Return normalization ---
        if not seq_children:
            return Node("NULL")
        if len(seq_children) == 1:
            return seq_children[0]
        return Node("SEQ", None, seq_children)
 
    # ---------- Main sequence using a QUEUE (backbone) ----------
    def build_pst_main(start_node):
        """Top-level SEQ via queue-like traversal; splits become nested blocks; loopbacks handled per-branch."""
        seq_children = []
        q = deque([start_node])
        seen_backbone = set()
        active_split_stack = []  # only used when expanding branches, not for the backbone queue
 
        while q:
            current = q.popleft()
            if current in seen_backbone:
                # Re-entering backbone node → treat as loop close; stop backbone here
                seq_children.append(Node("LOOPBACK", label_by_id(current)))
                break
            seen_backbone.add(current)
 
            # TASK
            if current in tasks:
                seq_children.append(Node("TASK", label(tasks[current])))
                nxts = succ.get(current, [])
                if len(nxts) == 1:
                    q.append(nxts[0])
                elif len(nxts) > 1:
                    # A task with multiple outs → treat as XOR split (rare)
                    kind = "XOR"
                    join = find_join(current, kind)
                    # Enter split scope
                    active_split_stack.append(current)
                    kids = []
                    for o in succ[current]:
                        if join and o == join:
                            kids.append(Node("NULL"))
                        else:
                            kids.append(build_path_until(o, join, active_split_stack.copy()))
                    seq_children.append(Node(kind, label(tasks[current]), kids))
                    active_split_stack.pop()
                    if join:
                        after = succ.get(join, [])
                        if after:
                            q.append(after[0])
                continue
 
            # EVENT
            if current in events:
                seq_children.append(Node("EVENT", label(events[current])))
                nxts = succ.get(current, [])
                if nxts:
                    q.append(nxts[0])
                continue
 
            # GATEWAY
            if current in gateways:
                if outdeg(current) > 1:
                    kind = "XOR" if is_xor(current) else ("AND" if is_and(current) else "XOR")
                    join = find_join(current, kind)
 
                    # Enter split scope (current can receive loopback from deeper branches)
                    active_split_stack.append(current)
 
                    kids = []
                    if join is None:
                        for o in succ[current]:
                            kids.append(build_path_until(o, None, active_split_stack.copy()))
                        seq_children.append(Node(kind, label(gateways[current]), kids))
                        active_split_stack.pop()
                        break
                    else:
                        for o in succ[current]:
                            if o == join:
                                kids.append(Node("NULL"))
                            else:
                                kids.append(build_path_until(o, join, active_split_stack.copy()))
                        seq_children.append(Node(kind, label(gateways[current]), kids))
                        active_split_stack.pop()
                        after = succ.get(join, [])
                        if after:
                            q.append(after[0])
                    continue
                else:
                    # Merge/pass-through
                    nxts = succ.get(current, [])
                    if nxts:
                        q.append(nxts[0])
                    continue
 
            # Unknown → stop
            break
 
        if not seq_children:
            return Node("NULL")
        if len(seq_children) == 1:
            return seq_children[0]
        return Node("SEQ", None, seq_children)
 
    return build_pst_main(start_id)
 
# ---------------- Entry point ----------------
if __name__ == "__main__":
    pst = parse_bpmn("test1.bpmn")
    with open("test1_pst.txt", "w", encoding="utf-8") as f:
        f.write(repr(pst))
    print("Process Structure Tree wurde in test1_pst.txt gespeichert.")