"""In-memory immutable object store shared by review job tests."""

import io

import app.storage as storage_module
from app.db import get_session_factory
from botocore.exceptions import ClientError
from custombuild_worker import tasks


def install_review_storage(monkeypatch):
    """Replace object storage and bind the worker to the test database."""

    class ReviewStorage:
        def __init__(self):
            self.objects = {}
            self.put_calls = []

        def head_bucket(self, **kwargs):
            pass

        def put_object(self, **kwargs):
            self.put_calls.append(kwargs)
            assert kwargs["IfNoneMatch"] == "*"
            key = kwargs["Key"]
            if key in self.objects:
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[key] = {
                "ContentLength": len(kwargs["Body"]),
                "ContentType": kwargs["ContentType"],
                "Metadata": kwargs["Metadata"],
                "payload": bytes(kwargs["Body"]),
            }
            return {"ETag": '"created"'}

        def head_object(self, **kwargs):
            return {
                key: value for key, value in self.objects[kwargs["Key"]].items() if key != "payload"
            }

        def get_object(self, **kwargs):
            return {
                **self.head_object(**kwargs),
                "Body": io.BytesIO(self.objects[kwargs["Key"]]["payload"]),
            }

    storage = ReviewStorage()
    monkeypatch.setattr(tasks, "SessionFactory", get_session_factory())
    monkeypatch.setattr(tasks, "_s3_client", lambda: storage)
    monkeypatch.setattr(storage_module, "internal_s3_client", lambda: storage)
    return storage
