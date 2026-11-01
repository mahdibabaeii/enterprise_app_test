from fastapi.testclient import TestClient


def test_app_serves_api_docs(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
