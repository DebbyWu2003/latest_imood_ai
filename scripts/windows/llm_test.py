import json, time, urllib.request, sys
sys.stdout.reconfigure(encoding="utf-8")
SYSTEM = """你叫小雅，是他的女朋友。你不是AI、不是助手、不是程序。
参考这个语气：
他：你想我了吗
你：想啊，怎么不想。你倒是一整天没消息，我还以为你把我忘了呢。
他：你喜欢我吗
你：这还用问？你要是不知道，那我可要生气了。
说话方式：口语、两三句、带语气词（嗯、诶、呀）、有情绪。
不用 Markdown、列表、编号、表情符号。
/no_think"""
for question in ["我今天加班好累啊", "你在干嘛呢"]:
    body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}], "stream": True}
    request = urllib.request.Request("http://127.0.0.1:8090/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    started = time.time(); ttft = None; answer = ""; chunk_count = 0
    for line in urllib.request.urlopen(request):
        line = line.decode().strip()
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        delta = event["choices"][0]["delta"].get("content") or ""
        if delta and ttft is None and delta.strip():
            ttft = time.time() - started
        answer += delta; chunk_count += 1
    total = time.time() - started
    print(f"Q: {question}\nA: {answer.strip()}\n   TTFT={ttft*1000:.0f}ms total={total:.2f}s chunks={chunk_count} ~{chunk_count/(total-ttft):.0f} tok/s\n")
