import xml.etree.ElementTree as ET
from xml.dom import minidom
import re

# ------------------- Node class -------------------
class Node:
    def __init__(self, ntype, value=None, children=None):
        self.ntype = ntype
        self.value = value
        self.children = children or []
    def __repr__(self): return f"{self.ntype}({self.value})"

# ------------------- Parse PST .txt -------------------
def parse_pst_text(text):
    text = text.strip()

    def parse_block(s):
        # Match node like TASK(xxx), or SEQ( ... )
        m = re.match(r'(\w+)\((.*)\)', s, re.S)
        if not m:
            return None
        ntype, inside = m.groups()
        inside = inside.strip()
        if ntype in ("TASK", "EVENT", "NULL"):
            # Extract simple value (e.g. TASK(abc))
            val = inside.rstrip(",")
            return Node(ntype, val or None)
        else:
            # complex node (SEQ, XOR, LOOP, AND)
            children = []
            depth = 0
            buff = ''
            for c in inside:
                if c == '(':
                    depth += 1
                    buff += c
                elif c == ')':
                    depth -= 1
                    buff += c
                elif c == ',' and depth == 0:
                    if buff.strip():
                        children.append(parse_block(buff.strip()))
                    buff = ''
                else:
                    buff += c
            if buff.strip():
                children.append(parse_block(buff.strip()))
            return Node(ntype, None, [ch for ch in children if ch])
    return parse_block(text)

# ------------------- BPMN Builder -------------------
class BPMNBuilder:
    def __init__(self):
        self.ns = "http://www.omg.org/spec/BPMN/20100524/MODEL"
        self.di_ns = "http://www.omg.org/spec/BPMN/20100524/DI"
        self.omgdc = "http://www.omg.org/spec/DD/20100524/DC"
        self.omgdi = "http://www.omg.org/spec/DD/20100524/DI"

        # register prefixes to match provided BPMN2 sample (bpmn2, bpmndi, dc, di)
        ET.register_namespace("bpmn2", self.ns)
        ET.register_namespace("bpmndi", self.di_ns)
        ET.register_namespace("dc", self.omgdc)
        ET.register_namespace("di", self.omgdi)

        self.doc = ET.Element(f"{{{self.ns}}}definitions", {
            "id": "Definitions_1",
            "targetNamespace": "http://example.bpmn"
        })
        self.process = ET.SubElement(self.doc, f"{{{self.ns}}}process",
                                     {"id": "Process_1", "isExecutable": "false"})
        self.diagram = ET.SubElement(self.doc, f"{{{self.di_ns}}}BPMNDiagram", {"id": "BPMNDiagram_1"})
        self.plane = ET.SubElement(self.diagram, f"{{{self.di_ns}}}BPMNPlane", {"bpmnElement": "Process_1"})

        self.next_id = 1
        # mapping from element id -> Element object in process (so we can add incoming/outgoing)
        self.elements = {}
        # mapping from element id -> bounds (x,y,width,height)
        self.shapes_info = {}

    def new_id(self, prefix):
        i = self.next_id
        self.next_id += 1
        return f"{prefix}_{i}"

    def add_shape(self, elem_id, x, y, w, h, is_marker=False):
        # create a BPMNShape in the diagram plane and record bounds
        attrs = {"bpmnElement": elem_id}
        if is_marker:
            attrs["isMarkerVisible"] = "true"
        shape = ET.SubElement(self.plane, f"{{{self.di_ns}}}BPMNShape", attrs)
        ET.SubElement(shape, f"{{{self.omgdc}}}Bounds",
                      {"x": str(x), "y": str(y), "width": str(w), "height": str(h)})
        # store bounds for edge waypoint calculations (left/top/width/height)
        self.shapes_info[elem_id] = {"x": float(x), "y": float(y), "w": float(w), "h": float(h)}

    def add_flow(self, src, tgt):
        # create sequenceFlow in process and add incoming/outgoing refs
        fid = self.new_id("Flow")
        flow = ET.SubElement(self.process, f"{{{self.ns}}}sequenceFlow",
                      {"id": fid, "sourceRef": src, "targetRef": tgt})

        # add outgoing to source element, incoming to target element (if elements known)
        src_elem = self.elements.get(src)
        tgt_elem = self.elements.get(tgt)
        if src_elem is not None:
            out = ET.SubElement(src_elem, f"{{{self.ns}}}outgoing")
            out.text = fid
        if tgt_elem is not None:
            inc = ET.SubElement(tgt_elem, f"{{{self.ns}}}incoming")
            inc.text = fid

        # create BPMNEdge with two waypoints (from right edge of source to left edge of target)
        # only if we have bounds for both elements
        s = self.shapes_info.get(src)
        t = self.shapes_info.get(tgt)
        if s and t:
            edge_id = self.new_id("Edge")
            edge = ET.SubElement(self.plane, f"{{{self.di_ns}}}BPMNEdge", {"id": edge_id, "bpmnElement": fid})
            sx = s["x"] + s["w"]
            sy = s["y"] + s["h"] / 2
            tx = t["x"]
            ty = t["y"] + t["h"] / 2
            ET.SubElement(edge, f"{{{self.omgdi}}}waypoint", {"x": str(sx), "y": str(sy)})
            ET.SubElement(edge, f"{{{self.omgdi}}}waypoint", {"x": str(tx), "y": str(ty)})
        return fid

    # Recursive PST → BPMN conversion
    # Returns tuple (entry_id, exit_id, x, y) where entry_id is where incoming flows should attach
    # and exit_id is where outgoing flows should originate for sequence chaining.
    def build(self, node, x=100, y=100):
        if node.ntype == "SEQ":
            first_entry = None
            prev_exit = None
            for child in node.children:
                entry, exit, x, y = self.build(child, x, y)
                if prev_exit:
                    # connect previous child's exit to this child's entry
                    self.add_flow(prev_exit, entry)
                if first_entry is None:
                    first_entry = entry
                prev_exit = exit
                x += 200
            return first_entry or prev_exit, prev_exit, x, y

        elif node.ntype == "TASK":
            elem_id = self.new_id("Task")
            elem = ET.SubElement(self.process, f"{{{self.ns}}}task",
                          {"id": elem_id, "name": node.value or "Task"})
            self.elements[elem_id] = elem
            self.add_shape(elem_id, x, y, 100, 80)
            return elem_id, elem_id, x, y

        elif node.ntype == "EVENT":
            elem_id = self.new_id("Event")
            name = node.value or "Event"
            tag = "startEvent" if "start" in name.lower() else (
                  "endEvent" if "end" in name.lower() else "intermediateCatchEvent")
            elem = ET.SubElement(self.process, f"{{{self.ns}}}{tag}",
                          {"id": elem_id, "name": name})
            self.elements[elem_id] = elem
            self.add_shape(elem_id, x, y, 36, 36)
            return elem_id, elem_id, x, y

        elif node.ntype in ("XOR", "AND"):
            kind = "exclusiveGateway" if node.ntype == "XOR" else "parallelGateway"
            # entry split
            split_id = self.new_id("Split")
            split_elem = ET.SubElement(self.process, f"{{{self.ns}}}{kind}",
                          {"id": split_id, "name": node.ntype})
            self.elements[split_id] = split_elem
            self.add_shape(split_id, x, y, 50, 50, is_marker=True)

            branch_y = y - (len(node.children) - 1) * 100
            branch_exits = []
            for ch in node.children:
                # build child branch; get its entry and exit
                entry, exit, _, _ = self.build(ch, x + 250, branch_y)
                # connect split -> child's entry
                self.add_flow(split_id, entry)
                branch_exits.append(exit)
                branch_y += 200

            # join back
            join_id = self.new_id("Join")
            join_elem = ET.SubElement(self.process, f"{{{self.ns}}}{kind}",
                          {"id": join_id, "name": "Join"})
            self.elements[join_id] = join_elem
            self.add_shape(join_id, x + 500, y, 50, 50, is_marker=True)
            for exit_id in branch_exits:
                self.add_flow(exit_id, join_id)

            # return entry=split, exit=join
            return split_id, join_id, x + 500, y

        elif node.ntype == "LOOP":
            # assume children[0] is condition, children[1] is body
            cond_entry, cond_exit, _, _ = self.build(node.children[0], x, y)
            body_entry, body_exit, _, _ = self.build(node.children[1], x + 200, y + 100)
            # link cond -> body and body -> cond to form loop
            self.add_flow(cond_exit, body_entry)
            self.add_flow(body_exit, cond_entry)  # loop back edge
            # entry is cond_entry, exit is cond_exit (after loop finishes)
            return cond_entry, cond_exit, x + 400, y + 200

        elif node.ntype == "NULL":
            elem_id = self.new_id("Null")
            elem = ET.SubElement(self.process, f"{{{self.ns}}}task", {"id": elem_id, "name": node.value or "None"})
            self.elements[elem_id] = elem
            self.add_shape(elem_id, x, y, 80, 60)
            return elem_id, elem_id, x, y

        else:
            elem_id = self.new_id("Unknown")
            elem = ET.SubElement(self.process, f"{{{self.ns}}}task", {"id": elem_id, "name": node.value or node.ntype})
            self.elements[elem_id] = elem
            self.add_shape(elem_id, x, y, 100, 80)
            return elem_id, elem_id, x, y

    def save(self, filename):
        xml_str = ET.tostring(self.doc, encoding="utf-8")
        pretty = minidom.parseString(xml_str).toprettyxml(indent="  ")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(pretty)
        print(f"✅ BPMN diagram saved as {filename}")

# ------------------- Main -------------------
if __name__ == "__main__":
    with open("test1_pst.txt", "r", encoding="utf-8") as f:
        pst_text = f.read()

    pst = parse_pst_text(pst_text)
    builder = BPMNBuilder()
    builder.build(pst)
    builder.save("test1_result.bpmn")