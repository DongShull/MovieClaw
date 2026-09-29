"""假模型网关（OpenAI 兼容）：给 AI 字幕生成的端到端演练用，不需要真实模型 Key。

能看懂字幕管线发出的三类请求并给出合规回答：术语表（glossary）、逐块翻译
（translate，单语/双语）与超读速压缩（compress）；其余请求（保存供应商时的连接
测试）回 pong。「译文」是确定性的：``译文{序号}号``，端到端脚本据此核对每一条
字幕都落在原来的时间轴上、没有错位。

故障可以在运行中通过 ``POST /control`` 打开，用来验证字幕任务的容错：

- ``latency``：每个请求的固定延迟（秒），让翻译过程可观察、可在中途打断；
- ``rate_limit_after`` / ``rate_limit_seconds``：第 N 个翻译请求起进入一段
  「限流风暴」，期间一律 429 + ``Retry-After: 1``（OpenAI SDK 自带 2 次重试，
  只有持续一段时间才会真正落到字幕任务的降速逻辑上）；
- ``error_after`` / ``error_seconds``：同理的一段 503 风暴（上游故障）；
- ``truncate_over``：条数超过它的块，首次请求按「写到输出上限」截断
  （finish_reason=length），验证对半拆块重译；
- ``garbage_every``：每 N 个翻译请求回一次缺条目的 JSON，验证结构校验与重试；
- ``long_every``：每 N 条译文故意写得很长，触发超读速压缩。

``GET /stats`` 返回调用明细：各用途的请求数与结果、每条对白被成功交付了几次
（重启续传后同一条被重复翻译 = 重复花钱，这是端到端要核对的关键数字）。

用法：``python scripts/perf/fake_llm_gateway.py --port 18812``
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()

CONTROL: dict[str, float | int | None] = {
    "latency": 0.0,
    "rate_limit_after": None,
    "rate_limit_seconds": 4.0,
    "error_after": None,
    "error_seconds": 4.0,
    "truncate_over": None,
    "garbage_every": None,
    "long_every": None,
}
STATS: dict[str, object] = {}
_storm_until = {"rate_limit": 0.0, "error": 0.0}
_seen_blocks: set[tuple[int, int]] = set()


def _reset_stats() -> None:
    STATS.clear()
    STATS.update(
        {
            "requests": Counter(),  # purpose:outcome → 次数
            "delivered": Counter(),  # 对白序号 → 成功交付次数
            "translate_requests": 0,
            "timeline": [],  # (时间, 用途, 条数, 结果)
        }
    )
    _storm_until.update({"rate_limit": 0.0, "error": 0.0})
    _seen_blocks.clear()


_reset_stats()


def _completion(model: str, content: str, *, finish_reason: str = "stop") -> JSONResponse:
    tokens = max(1, len(content) // 2)
    return JSONResponse(
        {
            "id": "fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": tokens * 3,
                "completion_tokens": tokens,
                "total_tokens": tokens * 4,
            },
        }
    )


def _error(status: int, message: str, kind: str) -> JSONResponse:
    headers = {"Retry-After": "1"} if status == 429 else {}
    return JSONResponse(
        {"error": {"message": message, "type": kind, "code": kind}},
        status_code=status,
        headers=headers,
    )


def _record(purpose: str, count: int, outcome: str) -> None:
    STATS["requests"][f"{purpose}:{outcome}"] += 1  # type: ignore[index]
    STATS["timeline"].append((round(time.time(), 2), purpose, count, outcome))  # type: ignore[union-attr]


def _payload_after(marker: str, text: str) -> list[dict]:
    start = text.index(marker) + len(marker)
    return json.loads(text[start:].strip())


def _translate(items: list[dict], bilingual: bool) -> list[dict]:
    long_every = CONTROL["long_every"]
    out = []
    for item in items:
        index = int(item["i"])
        text = f"译文{index}号"
        if long_every and index % int(long_every) == 0:
            text += "，这一句故意写得非常非常长，长到在字幕显示的时间里根本读不完"
        if bilingual:
            text = f"{text}\n{item['s']}"
        out.append({"i": index, "t": text})
    return out


@app.post("/control")
async def control(request: Request) -> dict:
    body = await request.json()
    if body.pop("reset_stats", False):
        _reset_stats()
    CONTROL.update(body)
    return {"control": CONTROL}


@app.get("/stats")
async def stats() -> dict:
    delivered: Counter = STATS["delivered"]  # type: ignore[assignment]
    return {
        "requests": dict(STATS["requests"]),  # type: ignore[arg-type]
        "translate_requests": STATS["translate_requests"],
        "delivered_lines": len(delivered),
        "duplicate_lines": sum(1 for count in delivered.values() if count > 1),
        "timeline": STATS["timeline"],
    }


@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    body = await request.json()
    model = body.get("model", "fake-model")
    messages = body.get("messages", [])
    system = next((m["content"] for m in messages if m.get("role") == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if CONTROL["latency"]:
        await asyncio.sleep(float(CONTROL["latency"]))

    if "找出其中反复出现的人名" in user:
        _record("glossary", 0, "ok")
        return _completion(model, json.dumps({"items": [{"src": "Neo", "dst": "尼奥"}]}))

    if "压缩改写" in user:
        # 提示词里有格式示例 [{"i": 序号, ...}]，待压缩清单是最后一行的 JSON 数组
        items = json.loads(user.rstrip().rsplit("\n", 1)[-1])
        shorter = [{"i": item["i"], "t": item["t"][: max(4, item["max_chars"])]} for item in items]
        _record("compress", len(items), "ok")
        return _completion(model, json.dumps({"items": shorter}, ensure_ascii=False))

    if "翻译下列对白：" not in user:
        _record("ping", 0, "ok")
        return _completion(model, "pong")

    items = _payload_after("翻译下列对白：", user)
    bilingual = "双语格式" in system
    STATS["translate_requests"] += 1  # type: ignore[operator]
    seq = STATS["translate_requests"]
    now = time.monotonic()
    for kind in ("rate_limit", "error"):
        after = CONTROL[f"{kind}_after"]
        if after is not None and seq == int(after):
            _storm_until[kind] = now + float(CONTROL[f"{kind}_seconds"] or 0)
    if now < _storm_until["rate_limit"]:
        _record("translate", len(items), "429")
        return _error(429, "Rate limit reached (fake gateway storm)", "rate_limit_exceeded")
    if now < _storm_until["error"]:
        _record("translate", len(items), "503")
        return _error(503, "Upstream overloaded (fake gateway storm)", "server_error")

    signature = (int(items[0]["i"]), len(items))
    truncate_over = CONTROL["truncate_over"]
    if truncate_over and len(items) > int(truncate_over) and signature not in _seen_blocks:
        _seen_blocks.add(signature)
        _record("translate", len(items), "truncated")
        partial = json.dumps({"items": _translate(items, bilingual)}, ensure_ascii=False)
        return _completion(model, partial[: len(partial) // 2], finish_reason="length")
    garbage_every = CONTROL["garbage_every"]
    if garbage_every and seq % int(garbage_every) == 0:
        _record("translate", len(items), "garbage")
        broken = _translate(items, bilingual)[:-1]  # 少一条：结构校验必须拦下并重试
        return _completion(model, json.dumps({"items": broken}, ensure_ascii=False))

    translated = _translate(items, bilingual)
    for item in translated:
        STATS["delivered"][item["i"]] += 1  # type: ignore[index]
    _record("translate", len(items), "ok")
    return _completion(model, json.dumps({"items": translated}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=18812)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
