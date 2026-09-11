"""One bounded-child-process Secrets Manager read using only the EC2 instance role."""

import re
import sys

import boto3
from botocore.config import Config
from botocore.credentials import InstanceMetadataProvider
from botocore.utils import InstanceMetadataFetcher


def main() -> int:
    if len(sys.argv) != 4:
        return 2
    arn, version, prefix = sys.argv[1:]
    match = re.fullmatch(r"arn:aws:secretsmanager:([a-z0-9-]+):([0-9]{12}):secret:[A-Za-z0-9/_+=.@-]+/", prefix)
    if match is None or not arn.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9-]{32,64}", version):
        return 2
    try:
        provider = InstanceMetadataProvider(iam_role_fetcher=InstanceMetadataFetcher(
            timeout=1, num_attempts=1, base_url="http://169.254.169.254/", env={},
            config={"ec2_metadata_v1_disabled": True},
        ))
        credentials = provider.load()
        if credentials is None:
            return 1
        frozen = credentials.get_frozen_credentials()
        session = boto3.Session(aws_access_key_id=frozen.access_key, aws_secret_access_key=frozen.secret_key,
                                aws_session_token=frozen.token, region_name=match[1])
        client = session.client("secretsmanager", config=Config(connect_timeout=1, read_timeout=1,
                                                                 retries={"total_max_attempts": 1}, proxies={}))
        try:
            response = client.get_secret_value(SecretId=arn, VersionId=version)
        finally:
            client.close()
        value = response.get("SecretString")
        if response.get("ARN") != arn or response.get("VersionId") != version:
            return 1
        if not isinstance(value, str) or not 32 <= len(value) <= 4096 or any(not 33 <= ord(char) <= 126 for char in value):
            return 1
        sys.stdout.write(value)
        return 0
    except Exception:
        # This child crosses SDK credential/response boundaries; never expose their exceptions.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
