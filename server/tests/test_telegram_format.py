from __future__ import annotations

from kru_agent.telegram import TelegramBot, TelegramError
from kru_agent.telegram_format import balance_fences, to_html, to_plain


def test_markdown_to_telegram_html():
    out = to_html("## Підсумок\n**Готово:** 3 з *5*, a < b & c\n- `x<1` і [сайт](https://e.com/?a=1&b=2)\n"
                  "- snake_case_name, 2*3*4\n> цитата")
    assert out.startswith("<b>Підсумок</b>\n<b>Готово:</b> 3 з <i>5</i>, a &lt; b &amp; c\n")
    assert '• <code>x&lt;1</code> і <a href="https://e.com/?a=1&amp;b=2">сайт</a>' in out
    assert "• snake_case_name, 2*3*4" in out  # не курсив посеред слів і чисел
    assert out.endswith("<blockquote>цитата</blockquote>")


def test_table_and_code_become_monospace():
    out = to_html("| Назва | Ціна |\n|---|---:|\n| **Кава** | 50 |\n| Чай | 5 |\n\n```py\nif a<b: pass\n```")
    assert "<pre>Назва │ Ціна\n──────┼─────\nКава  │ 50\nЧай   │ 5</pre>" in out
    assert '<pre><code class="language-py">if a&lt;b: pass</code></pre>' in out


def test_code_block_split_between_messages_stays_closed():
    assert balance_fences(["вступ\n```py\nx = 1", "y = 2\n```\nкінець"]) == \
        ["вступ\n```py\nx = 1\n```", "```py\ny = 2\n```\nкінець"]
    assert to_plain("**Звіт** за [день](https://e.com)") == "Звіт за день"


async def test_send_markdown_falls_back_to_plain_text():
    bot = TelegramBot("t")
    sent = []

    async def call(method, **params):
        sent.append(params)
        if params.get("parse_mode") and len(sent) == 1:
            raise TelegramError("Telegram: Bad Request: can't parse entities")
        return {"message_id": len(sent)}

    bot._call = call
    await bot.send("1", "**жирний**", markdown=True)
    assert sent[0]["parse_mode"] == "HTML" and sent[0]["text"] == "<b>жирний</b>"
    assert "parse_mode" not in sent[1] and sent[1]["text"] == "**жирний**"
    sent.clear()
    await bot.send("1", "a < b")  # звичайні службові повідомлення — без розмітки
    assert sent == [{"chat_id": "1", "text": "a < b", "disable_web_page_preview": True}]
