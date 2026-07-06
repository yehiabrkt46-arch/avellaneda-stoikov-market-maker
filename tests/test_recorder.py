import json

from mm_bot.feed.recorder import JsonlRecorder


def test_records_messages_as_jsonl_lines(tmp_path):
    rec = JsonlRecorder(tmp_path, "20260706-120000")
    rec.record({"a": 1})
    rec.record({"b": [1, 2]})
    rec.close()
    lines = rec.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l) for l in lines] == [{"a": 1}, {"b": [1, 2]}]


def test_filename_contains_session_id(tmp_path):
    rec = JsonlRecorder(tmp_path, "20260706-120000")
    rec.close()
    assert rec.path.name == "raw-20260706-120000.jsonl"


def test_creates_data_dir(tmp_path):
    rec = JsonlRecorder(tmp_path / "nested" / "dir", "s1")
    rec.record({"x": 1})
    rec.close()
    assert rec.path.exists()


def test_flush_makes_lines_visible_before_close(tmp_path):
    rec = JsonlRecorder(tmp_path, "s2")
    rec.record({"x": 1})
    rec.flush()
    assert rec.path.read_text(encoding="utf-8").strip() == '{"x":1}'
    rec.close()
