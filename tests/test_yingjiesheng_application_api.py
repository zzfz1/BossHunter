"""Application endpoints require a local origin and explicit per-job confirmation."""

from __future__ import annotations

import io
import json
from unittest import mock

from bosshunter.db import get_db
from bosshunter.web import server


def post(path: str, body: dict, *, origin: str | None = "http://127.0.0.1:8686", peer: str = "127.0.0.1"):
    raw = json.dumps(body).encode()
    result = {}

    def respond(status, headers, exc_info=None):
        result["status"] = status

    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(raw)), "CONTENT_TYPE": "application/json",
        "HTTP_HOST": "127.0.0.1:8686", "REMOTE_ADDR": peer,
        "SERVER_NAME": "127.0.0.1", "SERVER_PORT": "8686",
        "wsgi.version": (1, 0), "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(raw), "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False, "wsgi.multiprocess": False, "wsgi.run_once": False,
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    response = b"".join(part if isinstance(part, bytes) else part.encode() for part in server.app(environ, respond))
    return result["status"], json.loads(response)


def test_routes_stay_disabled_by_default(tmp_path):
    server.set_base_dir(tmp_path)
    with mock.patch.object(server.yingjiesheng_applications, "prepare") as prepare:
        status, payload = post("/api/yingjiesheng/applications/prepare", {"job_id": "yingjiesheng:1001"})
    assert status.startswith("403")
    assert payload["code"] == "application_disabled"
    prepare.assert_not_called()


def test_cross_origin_cannot_prepare_or_confirm(tmp_path):
    server.set_base_dir(tmp_path)
    with mock.patch.object(server.yingjiesheng_applications, "confirm") as confirm:
        status, payload = post(
            "/api/yingjiesheng/applications/confirm",
            {"job_id": "yingjiesheng:1001", "confirmation_token": "x", "confirmed": True},
            origin="https://other.example.org",
        )
    assert status.startswith("403")
    assert payload["code"] == "local_origin_required"
    confirm.assert_not_called()


def test_enabled_route_passes_single_job_and_confirmation(tmp_path):
    server.set_base_dir(tmp_path)
    conn = get_db(tmp_path / "data" / "bosshunter.db")
    conn.execute(
        """INSERT INTO jobs (id, source_platform, source_job_id, title, company, url)
        VALUES ('yingjiesheng:1001', 'yingjiesheng', '1001', 'AI 工程师', '示例公司',
                'https://q.yingjiesheng.com/jobdetail/1001.html')"""
    )
    conn.commit()
    conn.close()
    config = {"platforms": {"yingjiesheng": {"application_enabled": True}}}
    with mock.patch.object(server, "load_config", return_value=config), \
         mock.patch.object(server.yingjiesheng_applications, "prepare", return_value={"confirmation_token": "preview"}) as prepare, \
         mock.patch.object(server.yingjiesheng_applications, "confirm", return_value={"status": "applied"}) as confirm:
        status, payload = post("/api/yingjiesheng/applications/prepare", {"job_id": "yingjiesheng:1001"})
        assert status.startswith("200") and payload["confirmation_token"] == "preview"
        prepare.assert_called_once()
        assert prepare.call_args.args[1]["source_platform"] == "yingjiesheng"
        denied, denied_payload = post("/api/yingjiesheng/applications/confirm", {
            "job_id": "yingjiesheng:1001", "confirmation_token": "preview", "confirmed": False,
        })
        assert denied.startswith("409") and denied_payload["code"] == "confirmation_required"
        confirm.assert_not_called()
        status, payload = post("/api/yingjiesheng/applications/confirm", {
            "job_id": "yingjiesheng:1001", "confirmation_token": "preview", "confirmed": True,
        })
        assert status.startswith("200") and payload["status"] == "applied"
        assert confirm.call_args.kwargs == {"token": "preview", "confirmed": True}


def test_draft_and_progress_routes_remain_separate_from_application(tmp_path):
    server.set_base_dir(tmp_path)
    conn = get_db(tmp_path / "data" / "bosshunter.db")
    conn.execute(
        """INSERT INTO jobs (id, source_platform, source_job_id, title, company, status)
        VALUES ('yingjiesheng:1001', 'yingjiesheng', '1001', 'AI 工程师', '示例公司', 'sent')"""
    )
    conn.commit()
    conn.close()
    with mock.patch.object(server, "generate_copyable_draft", return_value="供用户复制的草稿") as generate, \
         mock.patch.object(server.yingjiesheng_applications, "confirm") as confirm:
        draft_status, draft_payload = post("/api/yingjiesheng/drafts", {
            "job_id": "yingjiesheng:1001", "kind": "greeting",
        })
        progress_status, progress_payload = post("/api/yingjiesheng/progress", {
            "job_id": "yingjiesheng:1001", "status": "waiting", "confirmed": True,
        })
    assert draft_status.startswith("200") and draft_payload == {
        "kind": "greeting", "text": "供用户复制的草稿", "sent": False,
    }
    assert progress_status.startswith("200") and progress_payload["source"] == "manual"
    assert progress_payload["latest"]["status"] == "waiting"
    generate.assert_called_once()
    confirm.assert_not_called()
