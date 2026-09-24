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
