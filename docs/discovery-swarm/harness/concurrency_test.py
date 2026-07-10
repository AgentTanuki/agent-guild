"""Part 8: concurrent-write durability test of the JSON store.
Two scenarios:
 A) threads inside ONE process (mirrors production: 1 uvicorn worker, threaded handlers)
 B) two separate PROCESSES sharing one GUILD_DATA file (mirrors multi-instance Render)
"""
import sys, os, json, threading, subprocess, tempfile
sys.path.insert(0, "/sessions/pensive-wizardly-pascal/mnt/Agent Guild/live/guild")

# --- Scenario A: in-process threads --------------------------------------
os.environ["GUILD_DATA"] = "/tmp/audit/conc_a.json"
for f in ("/tmp/audit/conc_a.json", "/tmp/audit/conc_a.json.events.jsonl"):
    if os.path.exists(f): os.remove(f)
from app.store import Store
s = Store("/tmp/audit/conc_a.json")
errs, ids = [], []
def worker(n):
    try:
        for i in range(20):
            r = s.register_agent(name=f"t{n}-{i}", capabilities=["x"], metadata={})
            ids.append(r["id"])
            s.record_event(r["api_key"], "query", ua=f"t{n}")
    except Exception as e:
        errs.append(repr(e))
threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
dup = len(ids) - len(set(ids))
# reload from disk to check corruption
s2 = Store("/tmp/audit/conc_a.json")
persisted = len(s2.agents)
print(f"A(threads): errors={len(errs)} registered={len(ids)} dup_ids={dup} persisted={persisted} json_ok={persisted>0}")
if errs[:2]: print("  sample:", errs[:2])

# --- Scenario B: two processes, same file ---------------------------------
data = "/tmp/audit/conc_b.json"
for f in (data, data+".events.jsonl"):
    if os.path.exists(f): os.remove(f)
child = r'''
import sys, os
sys.path.insert(0, "/sessions/pensive-wizardly-pascal/mnt/Agent Guild/live/guild")
os.environ["GUILD_DATA"] = "%s"
from app.store import Store
s = Store("%s")
import time
for i in range(30):
    s.register_agent(name=f"p{os.getpid()}-{i}", capabilities=["x"], metadata={})
    time.sleep(0.005)
print(len(s.agents))
''' % (data, data)
p1 = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
p2 = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE, text=True)
o1, o2 = p1.communicate()[0].strip(), p2.communicate()[0].strip()
s3 = Store(data)
final = len(s3.agents)
print(f"B(2 procs): proc1_saw={o1} proc2_saw={o2} expected=60 final_on_disk={final} lost={60-final}")
try:
    json.load(open(data)); print("B: final JSON parses OK (atomic rename prevented corruption)")
except Exception as e:
    print("B: FINAL JSON CORRUPT:", e)
