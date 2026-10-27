"""UDP emotion service + client with a fake predictor (no GPU / model). Run: python -m imood.tests.test_emotion_udp"""
import asyncio

from imood.emotion.udp_client import EmotionClient
from imood.emotion.udp_service import serve

PORT = 18102


def fake_predict(text):
    return (4, 0.9) if "開心" in text else (3, 0.8)


async def main():
    transport = await serve(fake_predict, "127.0.0.1", PORT)
    cl = EmotionClient("127.0.0.1", PORT, timeout_ms=1000)
    assert await cl.classify("今天真的很開心呢") == (4, 0.9)
    assert await cl.classify("我好難過喔今天") == (3, 0.8)
    assert (await cl.classify("嗯"))[0] == 0                       # blocked by preprocessing (too short) -> default
    assert (await cl.classify("，。！？"))[0] == 0                 # punctuation only -> default
    # background usage: not done yet -> default, done -> value
    t = cl.start("今天真的很開心呢")
    assert cl.result(t) == 0
    await asyncio.sleep(0.3)
    assert cl.result(t) == 4
    # failure paths: nobody listening / slow server -> default, never raises
    dead = EmotionClient("127.0.0.1", PORT + 1, timeout_ms=200)
    assert await dead.classify("今天真的很開心呢") == (0, 0.0)
    transport.close()

    async def slow_pred_server():
        import time
        return await serve(lambda t: (time.sleep(0.5), (4, 1.0))[1], "127.0.0.1", PORT + 2)
    tr2 = await slow_pred_server()
    slow = EmotionClient("127.0.0.1", PORT + 2, timeout_ms=100)
    assert await slow.classify("今天真的很開心呢") == (0, 0.0)     # timeout -> default
    tr2.close()
    print("emotion udp: ALL PASS")


asyncio.run(main())
