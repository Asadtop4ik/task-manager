from app.handlers.project_discussion import _task_request


def test_task_from_discussion_uses_transcript_and_at_most_three_images() -> None:
    messages = [
        {
            "role": "user",
            "text": "Katalog haqida savol",
            "images": [
                {"file_id": "1", "mime": "image/jpeg", "size": 10},
                {"file_id": "2", "mime": "image/jpeg", "size": 10},
            ],
        },
        {"role": "assistant", "text": "Hozir shunday ishlaydi."},
        {
            "role": "user",
            "text": "Katalogga bo‘sh holat yozuvini qo‘sh",
            "images": [
                {"file_id": "3", "mime": "image/png", "size": 12},
                {"file_id": "4", "mime": "image/png", "size": 12},
            ],
        },
    ]
    text, images = _task_request(messages)
    assert "@codex" in text
    assert "Katalogga bo‘sh holat yozuvini qo‘sh" in text
    assert [image["file_id"] for image in images] == ["2", "3", "4"]


def test_new_task_does_not_copy_old_discussion_or_full_failure_card() -> None:
    messages = [
        {"role": "user", "text": "Old unrelated question"},
        {"role": "assistant", "text": "Old unrelated answer"},
        {"role": "user", "text": "Another old topic"},
        {"role": "assistant", "text": "Another old answer"},
        {"role": "user", "text": "Past task failed: " + "log " * 1000},
        {"role": "assistant", "text": "That failure does not prove deployment."},
        {"role": "user", "text": "Opus 5.5 xarajatini yangila"},
        {"role": "assistant", "text": "Narxlarni tekshiraman."},
        {"role": "user", "text": ".env.example ga tegma"},
        {"role": "assistant", "text": "Model va narx yangilanadi."},
    ]
    text, _ = _task_request(messages)
    assert "Old unrelated" not in text
    assert "Opus 5.5 xarajatini yangila" in text
    assert ".env.example ga tegma" in text
    assert len(text) < 2500
