"""
Phase 9B-8C: `S3ObjectStore.head` against the 403-for-missing behaviour OBSERVED on real Backblaze B2 (2026-10-09).

With a prefix-restricted application key, B2 answers HEAD of a missing object with 403 (and GET with 403 "Cannot access bucket")
instead of 404. A 403 may count as "missing" ONLY when a listing of exactly that name succeeds and does not contain it. Uses a FAKE
boto3 client (botocore's real ClientError); runs where botocore is installed (the backup image / Linux runner).
"""
from __future__ import annotations

import pytest

botocore_exceptions = pytest.importorskip("botocore.exceptions")

from src.deployment import backup_offsite as off  # noqa: E402

KEY = "napsedge-validation/artifacts/a/offsite.json"


def _error(code: str, status: int):
    return botocore_exceptions.ClientError({"Error": {"Code": code, "Message": "x"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "HeadObject")


class FakeClient:
    def __init__(self, *, head_error=None, listed=(), list_error=None, size=7):
        self.head_error, self.listed, self.list_error, self.size, self.list_calls = head_error, list(listed), list_error, size, []

    def head_object(self, Bucket, Key):
        if self.head_error:
            raise self.head_error
        return {"ContentLength": self.size}

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error:
            raise self.list_error
        return {"Contents": [{"Key": k, "Size": 1} for k in self.listed if k.startswith(kwargs["Prefix"])][: kwargs.get("MaxKeys", 1000)]}


def store(client) -> off.S3ObjectStore:
    s = off.S3ObjectStore.__new__(off.S3ObjectStore)
    s.bucket, s._client = "example-bucket", client
    return s


def test_present_object_is_reported_with_its_size():
    assert store(FakeClient(size=42)).head(KEY) == off.ObjectInfo(KEY, 42)


def test_404_is_missing_without_a_listing():
    client = FakeClient(head_error=_error("404", 404))
    assert store(client).head(KEY) is None and client.list_calls == []


@pytest.mark.parametrize("code", ["403", "Forbidden", "AccessDenied"])
def test_b2_403_for_a_missing_object_is_missing_only_after_an_exact_listing(code):
    client = FakeClient(head_error=_error(code, 403), listed=[KEY + ".other", "napsedge-validation/artifacts/b/offsite.json"])
    assert store(client).head(KEY) is None
    assert client.list_calls == [{"Bucket": "example-bucket", "Prefix": KEY, "MaxKeys": 1}]


def test_403_for_an_object_the_listing_shows_is_a_refusal_not_missing():
    with pytest.raises(off.StoreError, match=r"head failed \(ClientError 403\)"):
        store(FakeClient(head_error=_error("403", 403), listed=[KEY])).head(KEY)


def test_403_when_the_listing_also_fails_is_a_refusal_not_missing():
    with pytest.raises(off.StoreError, match=r"list failed \(ClientError AccessDenied\)"):
        store(FakeClient(head_error=_error("403", 403), list_error=_error("AccessDenied", 403))).head(KEY)


def test_other_errors_stay_refusals():
    client = FakeClient(head_error=_error("500", 500))
    with pytest.raises(off.StoreError, match=r"head failed \(ClientError 500\)"):
        store(client).head(KEY)
    assert client.list_calls == []
