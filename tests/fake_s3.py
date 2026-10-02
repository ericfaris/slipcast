"""In-memory stand-in for a boto3 S3 client (Python port of bookhunt's
test/fake-s3.js). Deliberately not named test_*.py so pytest doesn't collect it;
import it as ``from fake_s3 import FakeS3``.

Only the client methods app/storage.py uses are implemented, with the response
shapes real S3/R2 returns (e.g. ``Contents`` omitted from an empty listing).
"""
import base64
import hashlib
import threading
from datetime import datetime, timezone

from botocore.exceptions import ClientError


def client_error(code: str, message: str, status: int, op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message},
         "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class FakeS3:
    def __init__(self, page_size: int = 1000):
        self.page_size = page_size
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.hooks = {"before_list": None}
        self._failures: dict[str, list] = {}
        self._lock = threading.Lock()

    # --- test helpers ----------------------------------------------------------

    def calls_of(self, op: str) -> list[dict]:
        with self._lock:
            return [kw for name, kw in self.calls if name == op]

    def fail_next(self, op: str, exc: Exception | None = None, times: int | None = 1):
        """Make the next ``times`` calls of ``op`` raise (``times=None``: forever)."""
        with self._lock:
            self._failures[op] = [exc, times]

    def seed(self, key: str, data: bytes, last_modified: datetime | None = None,
             content_type: str = "application/octet-stream"):
        with self._lock:
            self.objects[key] = {
                "body": data,
                "etag": f'"{hashlib.md5(data).hexdigest()}"',
                "last_modified": last_modified or datetime.now(timezone.utc),
                "content_type": content_type,
            }

    def _enter(self, op: str, kwargs: dict):
        with self._lock:
            self.calls.append((op, dict(kwargs)))
            failure = self._failures.get(op)
            if failure:
                exc, times = failure
                if times is not None:
                    failure[1] -= 1
                    if failure[1] <= 0:
                        del self._failures[op]
                raise exc or client_error("InternalError", f"injected {op} failure", 500, op)

    # --- boto3 client surface --------------------------------------------------

    def put_object(self, **kw):
        self._enter("put_object", kw)
        body = kw.get("Body", b"")
        data = body.read() if hasattr(body, "read") else bytes(body)
        md5 = hashlib.md5(data)
        if kw.get("ContentMD5") and kw["ContentMD5"] != base64.b64encode(md5.digest()).decode():
            raise client_error("BadDigest", "Content-MD5 mismatch", 400, "PutObject")
        etag = f'"{md5.hexdigest()}"'
        with self._lock:
            self.objects[kw["Key"]] = {
                "body": data, "etag": etag,
                "last_modified": datetime.now(timezone.utc),
                "content_type": kw.get("ContentType"),
            }
        return {"ETag": etag}

    def delete_object(self, **kw):
        self._enter("delete_object", kw)
        with self._lock:
            self.objects.pop(kw["Key"], None)
        return {}

    def head_object(self, **kw):
        self._enter("head_object", kw)
        with self._lock:
            obj = self.objects.get(kw["Key"])
        if obj is None:
            raise client_error("404", "Not Found", 404, "HeadObject")
        return {"ContentLength": len(obj["body"]), "ETag": obj["etag"],
                "LastModified": obj["last_modified"], "ContentType": obj["content_type"]}

    def list_objects_v2(self, **kw):
        self._enter("list_objects_v2", kw)
        hook = self.hooks.get("before_list")
        if hook:
            hook(kw)
        prefix = kw.get("Prefix", "")
        with self._lock:
            keys = sorted(k for k in self.objects if k.startswith(prefix))
            start = int(kw.get("ContinuationToken") or 0)
            page = keys[start:start + self.page_size]
            contents = [{"Key": k, "Size": len(self.objects[k]["body"]),
                         "ETag": self.objects[k]["etag"],
                         "LastModified": self.objects[k]["last_modified"]} for k in page]
        resp = {"KeyCount": len(contents), "IsTruncated": start + self.page_size < len(keys)}
        if contents:
            resp["Contents"] = contents
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn=3600):
        self._enter("generate_presigned_url",
                    {"ClientMethod": ClientMethod, "Params": Params, "ExpiresIn": ExpiresIn})
        return (f"https://fake-r2.test/{Params['Bucket']}/{Params['Key']}"
                f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake&method={ClientMethod}")
