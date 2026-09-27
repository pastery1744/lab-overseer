"""Replay logged snapshots through another backend to compare decisions (Ollama vs Claude).

  python -m overseer.replay -c config.yaml --backend claude --last 50
"""
import argparse, collections, json, time
import yaml
from . import brain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/overseer/config.yaml")
    ap.add_argument("--backend", choices=["ollama", "claude"], required=True)
    ap.add_argument("--last", type=int, default=20)
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config))
    llm = dict(cfg["llm"], backend=a.backend)
    b = brain.make_backend(llm)
    with open(cfg["decision_log"]) as f:
        recs = [json.loads(l) for l in collections.deque(f, maxlen=a.last)]  # only parse the tail
    for r in recs:
        if not json.loads(r["prompt"])["failing_checks"]:
            continue  # all-green reviews aren't interesting to compare
        t0 = time.time()
        try:
            out = b.triage(r["prompt"])
        except Exception as e:
            out = {"error": str(e)}
        print(f"=== {time.strftime('%F %T', time.localtime(r['ts']))}")
        print(f"  was  [{r['backend']} {r['latency_s']}s]:", [(i['target'], i['tier'], i['action'], i['action_arg']) for i in r['output'].get('incidents', [])])
        print(f"  now  [{b.name()} {time.time()-t0:.1f}s]:", [(i['target'], i['tier'], i['action'], i['action_arg']) for i in out.get('incidents', [])] if 'incidents' in out else out)


if __name__ == "__main__":
    main()
