"""Parallel HTTP range downloader: python parallel_download.py URL OUT [connections]"""
import os, sys, threading, urllib.request, time

url, out = sys.argv[1], sys.argv[2]
n = int(sys.argv[3]) if len(sys.argv) > 3 else 48
CHUNK = 4 * 1024 * 1024
UA = {"User-Agent": "Mozilla/5.0"}

req = urllib.request.Request(url, headers=UA, method="HEAD")
with urllib.request.urlopen(req) as r:
    size = int(r.headers["Content-Length"])
    final = r.geturl()
print(f"size={size}", flush=True)

part = out + ".part"
done_file = out + ".done"
if os.path.exists(out) and not os.path.exists(part):
    # continue a partial single-stream download: whole chunks already on disk count as done
    have = os.path.getsize(out)
    os.replace(out, part)
    with open(done_file, "w") as d:
        d.writelines(f"{i}\n" for i in range(0, size, CHUNK) if i + CHUNK <= have)
    with open(part, "r+b") as f:
        f.truncate(size)
elif not os.path.exists(part) or os.path.getsize(part) != size:
    open(part, "wb").close()
    if os.name == "nt":  # sparse => truncate is instant instead of zero-filling the whole file
        import subprocess
        subprocess.run(["fsutil", "sparse", "setflag", os.path.abspath(part)], check=True, capture_output=True)
    with open(part, "r+b") as f:
        f.truncate(size)
done_file = out + ".done"
done = set()
if os.path.exists(done_file):
    done = {int(x) for x in open(done_file).read().split()}
chunks = [i for i in range(0, size, CHUNK) if i not in done]
lock = threading.Lock()
cur_url = [final]


def refresh():
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD")) as r:
        cur_url[0] = r.geturl()


def worker():
    fh = open(part, "r+b")
    while True:
        with lock:
            if not chunks:
                break
            start = chunks.pop()
        end = min(start + CHUNK, size) - 1
        for attempt in range(20):
            try:
                h = dict(UA, Range=f"bytes={start}-{end}")
                with urllib.request.urlopen(urllib.request.Request(cur_url[0], headers=h), timeout=60) as r:
                    data = r.read()
                if len(data) != end - start + 1:
                    raise IOError("short read")
                fh.seek(start)
                fh.write(data)
                with lock:
                    done.add(start)
                    with open(done_file, "a") as d:
                        d.write(f"{start}\n")
                break
            except Exception as e:
                time.sleep(2)
                try:
                    refresh()
                except Exception:
                    pass
        else:
            print("FAILED chunk", start, flush=True)
    fh.close()


ts = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
for t in ts:
    t.start()
t0 = time.time()
while any(t.is_alive() for t in ts):
    time.sleep(30)
    print(f"{out}: {len(done) * CHUNK * 100 // size}% {len(done) * CHUNK / (time.time() - t0) / 1e6:.2f}MB/s", flush=True)
if all(i in done for i in range(0, size, CHUNK)):
    os.replace(part, out)
    os.remove(done_file)
    print("DONE", out, flush=True)
else:
    print("INCOMPLETE", out, flush=True)
