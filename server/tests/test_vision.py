from __future__ import annotations

import base64
import io
import json

import httpx
from PIL import Image

from kru_agent.agent import Agent
from kru_agent.images import MAX_SIDE, find_image_refs, load_image
from kru_agent.llm import OllamaClient, OpenAICompatClient, VisionNotSupported

from .conftest import FakeLLM, call, text
from .test_agent import Recorder


def _png(path, size=(64, 48), color=(255, 0, 0)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


def _images_in(messages):
    return [img for m in messages for img in (m.get("images") or [])]


def test_find_image_refs():
    text = "Що на uploads/скрін_1.png? І порівняй з /tmp/a.JPG, «b.webp». Не картинка: notes.txt"
    assert find_image_refs(text) == ["uploads/скрін_1.png", "/tmp/a.JPG", "b.webp"]


def test_load_image_downscales(tmp_path):
    _png(tmp_path / "big.png", size=(4000, 1000))
    img = load_image(tmp_path / "big.png")
    assert img["mime"] == "image/jpeg"
    w, h = Image.open(io.BytesIO(base64.b64decode(img["data"]))).size
    assert max(w, h) == MAX_SIDE


async def test_image_in_task_is_attached(cfg):
    _png(cfg.workspace / "uploads" / "скрін.png")
    llm = FakeLLM([text("Червоний прямокутник")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "Що на зображенні uploads/скрін.png?", rec.emit, rec.confirm)
    imgs = _images_in(llm.calls[0]["messages"])
    assert len(imgs) == 1 and imgs[0]["mime"].startswith("image/")
    assert "You can SEE images" in llm.calls[0]["messages"][0]["content"]
    # в історії зберігається лише шлях, не base64
    assert agent.storage.load_session("s")[0]["images"] == ["uploads/скрін.png"]


async def test_view_image_tool(cfg):
    _png(cfg.workspace / "chart.png")
    llm = FakeLLM([call("view_image", path="chart.png"), text("На графіку зростання")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "Подивись на графік у chart.png і скажи тренд", rec.emit, rec.confirm)
    second = llm.calls[1]["messages"]
    assert second[-2]["role"] == "tool" and "додано" in second[-2]["content"]
    assert second[-1]["role"] == "user" and len(second[-1]["images"]) == 1


async def test_only_recent_images_are_sent(cfg):
    cfg.max_images_in_context = 1
    for n in ("a", "b"):
        _png(cfg.workspace / f"{n}.png")
    llm = FakeLLM([text("перша"), text("друга")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "що на a.png", rec.emit, rec.confirm)
    await agent.run("s", "а на b.png?", rec.emit, rec.confirm)
    msgs = llm.calls[1]["messages"]
    assert len(_images_in(msgs)) == 1
    assert any("a.png — переглядалось раніше" in (m.get("content") or "") for m in msgs)


async def test_vision_not_supported_falls_back(cfg):
    _png(cfg.workspace / "x.png")
    llm = FakeLLM([VisionNotSupported("image input is not supported - you may need to provide the mmproj"), text("Не бачу картинку")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    answer = await agent.run("s", "опиши x.png", rec.emit, rec.confirm)
    assert answer == "Не бачу картинку" and not agent.vision
    retry = llm.calls[1]["messages"]
    assert not _images_in(retry)
    assert "поточна модель не бачить зображень" in retry[-1]["content"]


async def test_vision_off(cfg):
    cfg.vision = "off"
    _png(cfg.workspace / "x.png")
    llm = FakeLLM([text("ок")])
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "опиши x.png", rec.emit, rec.confirm)
    assert not _images_in(llm.calls[0]["messages"])
    assert "You can SEE images" not in llm.calls[0]["messages"][0]["content"]


async def test_openai_image_parts_and_vision_error():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(500, json={"error": {"message": "image input is not supported - hint: provide the mmproj"}})

    c = OpenAICompatClient("http://x:8085", "qwen")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    msgs = [{"role": "user", "content": "що тут?", "images": [{"mime": "image/png", "data": "QUJD"}]}]
    try:
        await c.chat(msgs)
        raise AssertionError("очікувалась VisionNotSupported")
    except VisionNotSupported:
        pass
    parts = seen["body"]["messages"][0]["content"]
    assert parts[0]["image_url"]["url"] == "data:image/png;base64,QUJD"  # картинка перед текстом
    assert parts[1] == {"type": "text", "text": "що тут?"}


async def test_ollama_images_payload():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=json.dumps({"message": {"role": "assistant", "content": "ок"}, "done": True}))

    c = OllamaClient("http://x", "qwen3.5:9b")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.chat([{"role": "user", "content": "що тут?", "images": [{"mime": "image/png", "data": "QUJD"}]}])
    assert seen["body"]["messages"][0]["images"] == ["QUJD"]


class ProbeLLM(FakeLLM):
    """FakeLLM, що відповідає на перевірку зору як llama-server (/props → modalities.vision)."""

    def __init__(self, script, vision):
        super().__init__(script)
        self.vision_flag = vision

    async def supports_vision(self):
        return self.vision_flag


async def test_vision_probe_and_events(cfg):
    _png(cfg.workspace / "uploads" / "s.png")
    llm = ProbeLLM([text("Бачу вікно терміналу")], vision=True)
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "Що на зображенні uploads/s.png?", rec.emit, rec.confirm)
    assert {"type": "vision", "state": "attached", "images": ["uploads/s.png"]} in rec.events
    user = llm.calls[0]["messages"][-1]
    assert user["content"].startswith("[Прикріплене зображення: uploads/s.png. Воно вже перед тобою")
    assert "NEVER try to open, OCR" in llm.calls[0]["messages"][0]["content"]


async def test_vision_unavailable_is_reported_and_recovers(cfg):
    _png(cfg.workspace / "s.png")
    llm = ProbeLLM([text("Зір вимкнено"), text("Тепер бачу")], vision=False)
    agent = Agent(cfg, llm=llm)
    rec = Recorder()
    await agent.run("s", "опиши s.png", rec.emit, rec.confirm)
    ev = next(e for e in rec.events if e["type"] == "vision")
    assert ev["state"] == "unavailable" and "mmproj" in ev["reason"]
    assert not _images_in(llm.calls[0]["messages"])
    assert "CANNOT see images" in llm.calls[0]["messages"][0]["content"]
    # модель перезапустили з mmproj — агент сам це помічає, без перезапуску
    llm.vision_flag = True
    agent._vision_checked = 0
    await agent.run("s", "а тепер опиши s.png", rec.emit, rec.confirm)
    assert agent.vision
    last = llm.calls[1]["messages"][-1]
    assert last["images"] and last["content"].startswith("[Прикріплене зображення")
    # картинка з першого повідомлення (коли зору не було) збереглася і тепер теж видна
    assert len(_images_in(llm.calls[1]["messages"])) == 2


async def test_probe_endpoints():
    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"vision": True, "audio": False}})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools"]})
        return httpx.Response(404)

    oa = OpenAICompatClient("http://x:8085", "qwen")
    oa._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await oa.supports_vision() is True
    ol = OllamaClient("http://x", "qwen3.5:9b")
    ol._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await ol.supports_vision() is False
