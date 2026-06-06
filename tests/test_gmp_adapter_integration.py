"""Integration test: parse_extension_payload applies nickname cleaning."""
from api.adapters.gmp import parse_extension_payload


def test_parse_extension_payload_cleans_nicknames():
    payload = {
        "provider": "gmp",
        "capturedAt": "2026-01-01T00:00:00Z",
        "user": {},
        "auth": {},
        "accounts": [
            {
                "accountNumber": "1",
                "customerNumber": "c1",
                "nickname": "1a_Chester",
                "currentBillUrl": None,
                "serviceAddress": None,
            },
            {
                "accountNumber": "2",
                "customerNumber": "c2",
                "nickname": "Tannery Brook",
                "currentBillUrl": None,
                "serviceAddress": None,
            },
            {
                "accountNumber": "3",
                "customerNumber": "c3",
                "nickname": "2b_Tannery Brook",
                "currentBillUrl": None,
                "serviceAddress": None,
            },
        ],
    }
    out = parse_extension_payload(payload)
    assert out["accounts"][0]["nickname"] == "Chester"
    assert out["accounts"][1]["nickname"] == "Tannery Brook"
    assert out["accounts"][2]["nickname"] == "Tannery Brook"
