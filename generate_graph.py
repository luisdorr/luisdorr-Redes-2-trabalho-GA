import json
import glob

edges = set()
nodes = set()

for path in glob.glob("config/r*.json"):  
    with open(path) as f:
        data = json.load(f)
    rid = data["router_id"]
    nodes.add(rid)
    for n in data.get("neighbors", []):
        nid = n["id"]
        nodes.add(nid)
        edge = tuple(sorted([rid, nid]))
        edges.add(edge)

with open("topologia.mermaid", "w") as f:
    f.write("graph LR\n")
    for a, b in sorted(edges):
        f.write(f"    {a} --- {b}\n")