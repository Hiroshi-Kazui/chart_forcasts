import json

from claude_harness import verify_client


class Response:
    status = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_http_200_with_failed_verification_returns_failure_and_prints_evidence(
    monkeypatch, capsys
) -> None:
    payload = {
        "success": False,
        "commands": [
            {
                "argv": ["python", "-c", "assert False"],
                "exit_code": 1,
                "timed_out": False,
                "stderr": "AssertionError",
            }
        ],
    }
    monkeypatch.setattr(verify_client.urllib.request, "urlopen", lambda *a, **k: Response(payload))

    result = verify_client.main(["--url", "http://127.0.0.1/verify", "--token", "test-token"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 1
    assert "exit_code" in output
    assert "AssertionError" in output
