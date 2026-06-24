from __future__ import annotations

import uuid
from dataclasses import dataclass

import aioboto3


@dataclass(frozen=True)
class S3UploadResult:
    key: str


class S3Client:
    def __init__(
        self,
        bucket: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._session = aioboto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )
        self._endpoint_url = endpoint_url

    async def upload_bytes(self, *, content: bytes, content_type: str, prefix: str) -> S3UploadResult:
        key = f"{prefix}/{uuid.uuid4().hex}"
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
            )
        return S3UploadResult(key=key)

    async def presign_get_url(self, key: str, expires_in: int = 600) -> str:
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )
