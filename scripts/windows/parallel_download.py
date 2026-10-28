"""Parallel HTTP range downloader: python parallel_download.py URL OUT [connections]"""
import os, sys, threading, urllib.request, time

source_url, output_path = sys.argv[1], sys.argv[2]
connection_count = int(sys.argv[3]) if len(sys.argv) > 3 else 48
CHUNK = 4 * 1024 * 1024
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0"}

head_request = urllib.request.Request(source_url, headers=REQUEST_HEADERS, method="HEAD")
with urllib.request.urlopen(head_request) as response:
    size = int(response.headers["Content-Length"])
    final_url = response.geturl()
print(f"size={size}", flush=True)

part_path = output_path + ".part"
done_path = output_path + ".done"
if os.path.exists(output_path) and not os.path.exists(part_path):
    # continue a partial single-stream download: whole chunks already on disk count as done
    bytes_on_disk = os.path.getsize(output_path)
    os.replace(output_path, part_path)
    with open(done_path, "w") as done_handle:
        done_handle.writelines(f"{i}\n" for i in range(0, size, CHUNK) if i + CHUNK <= bytes_on_disk)
    with open(part_path, "r+b") as part_handle:
        part_handle.truncate(size)
elif not os.path.exists(part_path) or os.path.getsize(part_path) != size:
    open(part_path, "wb").close()
    if os.name == "nt":  # sparse => truncate is instant instead of zero-filling the whole file
        import subprocess
        subprocess.run(["fsutil", "sparse", "setflag", os.path.abspath(part_path)], check=True, capture_output=True)
    with open(part_path, "r+b") as part_handle:
        part_handle.truncate(size)
done_path = output_path + ".done"
done = set()
if os.path.exists(done_path):
    done = {int(offset_text) for offset_text in open(done_path).read().split()}
chunks = [i for i in range(0, size, CHUNK) if i not in done]
lock = threading.Lock()
current_url = [final_url]


def refresh():
    with urllib.request.urlopen(urllib.request.Request(source_url, headers=REQUEST_HEADERS, method="HEAD")) as response:
        current_url[0] = response.geturl()


def worker():
    part_file = open(part_path, "r+b")
    while True:
        with lock:
            if not chunks:
                break
            chunk_start = chunks.pop()
        chunk_end = min(chunk_start + CHUNK, size) - 1
        for attempt in range(20):
            try:
                request_headers = dict(REQUEST_HEADERS, Range=f"bytes={chunk_start}-{chunk_end}")
                with urllib.request.urlopen(urllib.request.Request(current_url[0], headers=request_headers), timeout=60) as response:
                    data = response.read()
                if len(data) != chunk_end - chunk_start + 1:
                    raise IOError("short read")
                part_file.seek(chunk_start)
                part_file.write(data)
                with lock:
                    done.add(chunk_start)
                    with open(done_path, "a") as done_handle:
                        done_handle.write(f"{chunk_start}\n")
                break
            except Exception as error:
                time.sleep(2)
                try:
                    refresh()
                except Exception:
                    pass
        else:
            print("FAILED chunk", chunk_start, flush=True)
    part_file.close()


threads = [threading.Thread(target=worker, daemon=True) for _ in range(connection_count)]
for thread in threads:
    thread.start()
started = time.time()
while any(thread.is_alive() for thread in threads):
    time.sleep(30)
    print(f"{output_path}: {len(done) * CHUNK * 100 // size}% {len(done) * CHUNK / (time.time() - started) / 1e6:.2f}MB/s", flush=True)
if all(i in done for i in range(0, size, CHUNK)):
    os.replace(part_path, output_path)
    os.remove(done_path)
    print("DONE", output_path, flush=True)
else:
    print("INCOMPLETE", output_path, flush=True)
