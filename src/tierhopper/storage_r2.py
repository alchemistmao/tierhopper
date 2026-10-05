"""Cloudflare R2 (S3-compatible) storage for job packages, checkpoints, results and logs.

Jobs never receive R2 keys: the control plane hands them presigned URLs scoped to one object.

Layout:
    pkg/<job>.tar.gz                    job package (code + runner)
    ckpt/<job>/<shard>/latest.tar.gz    latest checkpoint of a shard
    results/<job>/<shard>/outputs.tar.gz
    logs/<attempt>.log
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from tierhopper import credentials

MAX_PRESIGN = timedelta(days=7)  # SigV4 hard limit


@dataclass(frozen=True)
class Keys:
    job: str

    @property
    def package(self) -> str:
        return f"pkg/{self.job}.tar.gz"

    def checkpoint(self, shard_idx: int) -> str:
        return f"ckpt/{self.job}/{shard_idx}/latest.tar.gz"

    def results(self, shard_idx: int) -> str:
        return f"results/{self.job}/{shard_idx}/outputs.tar.gz"

    @staticmethod
    def log(attempt_id: str) -> str:
        return f"logs/{attempt_id}.log"


def configured() -> bool:
    return all(credentials.get_secret("r2", f) for f in ("account_id", "access_key_id", "secret_access_key"))


class R2:
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        account = credentials.get_secret("r2", "account_id")
        key_id = credentials.get_secret("r2", "access_key_id")
        secret = credentials.get_secret("r2", "secret_access_key")
        if not (account and key_id and secret):
            raise RuntimeError("R2 is not configured; run `tierhopper config r2`")
        self.bucket = credentials.get_secret("r2", "bucket") or "tierhopper"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    # ---- direct access (control plane / Mac only) ---------------------------------------------
    def put(self, key: str, data: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 - 404 surfaces as ClientError
            return False

    def check(self) -> None:
        """Round-trip a tiny object to prove the credentials and bucket work."""
        self.put("healthcheck/ping", b"ok")
        if self.get("healthcheck/ping") != b"ok":
            raise RuntimeError("R2 round-trip failed")
        self.s3.delete_object(Bucket=self.bucket, Key="healthcheck/ping")

    # ---- presigned URLs for jobs --------------------------------------------------------------
    def presign_get(self, key: str, ttl: timedelta) -> str:
        return self.s3.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                              ExpiresIn=int(min(ttl, MAX_PRESIGN).total_seconds()))

    def presign_put(self, key: str, ttl: timedelta) -> str:
        return self.s3.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": key},
                                              ExpiresIn=int(min(ttl, MAX_PRESIGN).total_seconds()))
