from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_settings_and_chat_expose_nano_context_mode():
    settings = (ROOT / 'web/modules/settings_ui.js').read_text()
    chat = (ROOT / 'web/modules/chat.js').read_text()
    assert "{ value: 'nano', label: 'Nano' }" in settings
    assert 'data-mode="nano">Nano' in chat
    assert "['nano', 'low', 'max'].includes(data.context_mode)" in chat
    assert "['nano', 'low', 'max'].includes(seg.dataset.mode)" in chat
